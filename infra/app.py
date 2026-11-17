"""AWS infrastructure for the Python agents platform."""

import os

from aws_cdk import (
    App,
    CfnOutput,
    CfnParameter,
    Duration,
    Environment,
    Fn,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cognito as cognito,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
    aws_rds as rds,
    aws_route53 as route53,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
)
from constructs import Construct


AGENTS = ("CODE", "SECURITY", "DEVOPS", "INFRA", "MONITORING")


class AgentsPlatformStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, production: bool,
                 teardown: bool = False, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if production and not teardown:
            self.termination_protection = True
        persistent_policy = RemovalPolicy.DESTROY if teardown else RemovalPolicy.RETAIN

        certificate_arn = CfnParameter(self, "CertificateArn", type="String")
        domain_name = CfnParameter(self, "DomainName", type="String")
        hosted_zone_id = CfnParameter(self, "HostedZoneId", type="String")
        image_tag = CfnParameter(self, "ImageTag", type="String", default="bootstrap")
        migration_image_tag = CfnParameter(self, "MigrationImageTag", type="String", default="bootstrap")
        api_desired_count = CfnParameter(self, "ApiDesiredCount", type="Number", default=0)
        background_desired_count = CfnParameter(self, "BackgroundDesiredCount", type="Number", default=0)
        allowed_repos = CfnParameter(self, "AllowedRepositories", type="String", default="")
        allowed_pipelines = CfnParameter(self, "AllowedPipelines", type="String", default="")
        allowed_stacks = CfnParameter(self, "AllowedStacks", type="String", default="")
        openai_model = CfnParameter(self, "OpenAIModel", type="String", default="gpt-5.5")

        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=2 if production else 1)
        api_sg = ec2.SecurityGroup(self, "ApiSecurityGroup", vpc=vpc, allow_all_outbound=True)
        worker_sg = ec2.SecurityGroup(self, "WorkerSecurityGroup", vpc=vpc, allow_all_outbound=True)
        db_sg = ec2.SecurityGroup(self, "DatabaseSecurityGroup", vpc=vpc, allow_all_outbound=False)
        db_sg.add_ingress_rule(api_sg, ec2.Port.tcp(5432))
        db_sg.add_ingress_rule(worker_sg, ec2.Port.tcp(5432))

        database = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16),
            credentials=rds.Credentials.from_generated_secret("agents"),
            database_name="agents",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize.SMALL if production else ec2.InstanceSize.MICRO,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[db_sg],
            allocated_storage=20,
            storage_encrypted=True,
            multi_az=production,
            backup_retention=Duration.days(7),
            deletion_protection=production and not teardown,
            removal_policy=persistent_policy,
        )
        if database.secret:
            database.secret.apply_removal_policy(persistent_policy)

        queue_key = kms.Key(self, "QueueKey", enable_key_rotation=True,
                            removal_policy=persistent_policy)
        dead_letter_queue = sqs.Queue(
            self, "DeadLetterQueue", fifo=True, encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=queue_key, retention_period=Duration.days(14),
        )
        queues = {
            name: sqs.Queue(
                self, f"{name.title()}Queue", fifo=True,
                encryption=sqs.QueueEncryption.KMS, encryption_master_key=queue_key,
                visibility_timeout=Duration.seconds(120),
                dead_letter_queue=sqs.DeadLetterQueue(queue=dead_letter_queue, max_receive_count=5),
            )
            for name in (*AGENTS, "RESULTS")
        }

        repository = ecr.Repository(
            self, "Repository", removal_policy=persistent_policy,
            empty_on_delete=teardown,
        )
        image = ecs.ContainerImage.from_ecr_repository(repository, image_tag.value_as_string)
        migration_image = ecs.ContainerImage.from_ecr_repository(
            repository, migration_image_tag.value_as_string
        )
        github_secret = secretsmanager.Secret(self, "GitHubToken", removal_policy=persistent_policy)
        openai_secret = secretsmanager.Secret(self, "OpenAIKey", removal_policy=persistent_policy)
        pool = cognito.UserPool(self, "UserPool", self_sign_up_enabled=False,
                                removal_policy=persistent_policy)
        app_client = pool.add_client(
            "ApiClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
        )
        for group in ("agents-readers", "agents-operators", "agents-admins"):
            cognito.CfnUserPoolGroup(self, group.replace("-", "").title(),
                user_pool_id=pool.user_pool_id, group_name=group)

        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        common_env = {
            "AGENTS_MODE": "aws",
            "AWS_REGION": self.region,
            "DB_HOST": database.db_instance_endpoint_address,
            "DB_PORT": database.db_instance_endpoint_port,
            "DB_NAME": "agents",
            "DB_USER": "agents",
            "AGENTS_ALLOWED_REPOS": allowed_repos.value_as_string,
            "AGENTS_ALLOWED_PIPELINES": allowed_pipelines.value_as_string,
            "AGENTS_ALLOWED_STACKS": allowed_stacks.value_as_string,
            **{f"SQS_{name}_URL": queue.queue_url for name, queue in queues.items()},
        }
        db_secret = {"DB_PASSWORD": ecs.Secret.from_secrets_manager(database.secret, "password")}
        image_log = logs.LogGroup(
            self, "ApplicationLogs", retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=persistent_policy,
        )

        def task_definition(role: str, command: list[str] | None = None,
                            extra_env: dict[str, str] | None = None,
                            extra_secrets: dict[str, ecs.Secret] | None = None) -> ecs.FargateTaskDefinition:
            large_task = role in {"Api", "CodeWorker", "SecurityWorker"}
            task = ecs.FargateTaskDefinition(
                self, f"{role}Task",
                cpu=512 if large_task else 256,
                memory_limit_mib=1024 if large_task else 512,
            )
            task.add_volume(name="scratch")
            container = task.add_container(
                role,
                image=image,
                command=command,
                environment=common_env | (extra_env or {}),
                secrets=db_secret | (extra_secrets or {}),
                logging=ecs.LogDrivers.aws_logs(stream_prefix=role.lower(), log_group=image_log),
                readonly_root_filesystem=True,
            )
            container.add_mount_points(ecs.MountPoint(
                container_path="/tmp", source_volume="scratch", read_only=False
            ))
            if role == "Api":
                container.add_port_mappings(ecs.PortMapping(container_port=8000))
                container.add_environment("COGNITO_USER_POOL_ID", pool.user_pool_id)
                container.add_environment("COGNITO_APP_CLIENT_ID", app_client.user_pool_client_id)
            return task

        api_task = task_definition(
            "Api", extra_secrets={"GITHUB_TOKEN": ecs.Secret.from_secrets_manager(github_secret)}
        )
        api_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudwatch:GetMetricStatistics"], resources=["*"],
        ))
        api_pipeline_arns = self.node.try_get_context("codepipeline_arns") or []
        if api_pipeline_arns:
            api_task.task_role.add_to_policy(iam.PolicyStatement(
                actions=["codepipeline:GetPipelineExecution"], resources=api_pipeline_arns,
            ))
        api_stack_arns = self.node.try_get_context("managed_stack_arns") or []
        if api_stack_arns:
            api_task.task_role.add_to_policy(iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"], resources=api_stack_arns,
            ))
        migration_task = ecs.FargateTaskDefinition(
            self, "MigrationTask", cpu=256, memory_limit_mib=512
        )
        migration_task.add_container(
            "Migration", image=migration_image,
            command=["alembic", "upgrade", "head"],
            environment=common_env,
            secrets=db_secret,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="migration", log_group=image_log),
        )
        api_service = ecs.FargateService(
            self, "ApiService", cluster=cluster, task_definition=api_task,
            desired_count=api_desired_count.value_as_number,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[api_sg],
        )
        alb = elbv2.ApplicationLoadBalancer(self, "LoadBalancer", vpc=vpc, internet_facing=True)
        listener = alb.add_listener(
            "Https", port=443, certificates=[acm.Certificate.from_certificate_arn(
                self, "Certificate", certificate_arn.value_as_string
            )],
        )
        alb.add_listener("HttpRedirect", port=80, default_action=elbv2.ListenerAction.redirect(
            protocol="HTTPS", port="443", permanent=True
        ))
        route53.CfnRecordSet(
            self, "ApiDnsRecord", hosted_zone_id=hosted_zone_id.value_as_string,
            name=domain_name.value_as_string, type="A",
            alias_target=route53.CfnRecordSet.AliasTargetProperty(
                dns_name=alb.load_balancer_dns_name,
                hosted_zone_id=alb.load_balancer_canonical_hosted_zone_id,
                evaluate_target_health=True,
            ),
        )
        listener.add_targets(
            "ApiTargets", port=8000,
            targets=[api_service.load_balancer_target(container_name="Api", container_port=8000)],
            health_check=elbv2.HealthCheck(path="/health"),
        )
        api_sg.add_ingress_rule(alb.connections.security_groups[0], ec2.Port.tcp(8000))

        dispatcher_task = task_definition("Dispatcher", ["python", "-m", "agents_platform.runtime", "dispatcher"])
        for name in AGENTS:
            queues[name].grant_send_messages(dispatcher_task.task_role)
        ecs.FargateService(
            self, "DispatcherService", cluster=cluster, task_definition=dispatcher_task,
            desired_count=background_desired_count.value_as_number, security_groups=[worker_sg],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        result_task = task_definition("Results", ["python", "-m", "agents_platform.runtime", "results"])
        queues["RESULTS"].grant_consume_messages(result_task.task_role)
        ecs.FargateService(
            self, "ResultsService", cluster=cluster, task_definition=result_task,
            desired_count=background_desired_count.value_as_number, security_groups=[worker_sg],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        for name in AGENTS:
            extra_env = {"OPENAI_MODEL": openai_model.value_as_string} if name == "CODE" else {}
            extra_secrets = {}
            if name in {"CODE", "SECURITY", "DEVOPS"}:
                extra_secrets["GITHUB_TOKEN"] = ecs.Secret.from_secrets_manager(github_secret)
            if name == "CODE":
                extra_secrets["OPENAI_API_KEY"] = ecs.Secret.from_secrets_manager(openai_secret)
            task = task_definition(
                f"{name.title()}Worker",
                ["python", "-m", "agents_platform.runtime", "worker", "--agent", name],
                extra_env, extra_secrets,
            )
            queues[name].grant_consume_messages(task.task_role)
            queues["RESULTS"].grant_send_messages(task.task_role)
            if name == "DEVOPS":
                pipeline_arns = self.node.try_get_context("codepipeline_arns") or []
                if pipeline_arns:
                    task.task_role.add_to_policy(iam.PolicyStatement(
                        actions=["codepipeline:StartPipelineExecution", "codepipeline:GetPipelineExecution"],
                        resources=pipeline_arns,
                    ))
            if name == "INFRA":
                stack_arns = self.node.try_get_context("managed_stack_arns") or []
                if stack_arns:
                    task.task_role.add_to_policy(iam.PolicyStatement(
                        actions=["cloudformation:DescribeStacks", "cloudformation:CreateChangeSet",
                                 "cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet"],
                        resources=stack_arns,
                    ))
            if name == "MONITORING":
                task.task_role.add_to_policy(iam.PolicyStatement(
                    actions=["cloudwatch:GetMetricStatistics"], resources=["*"],
                ))
            ecs.FargateService(
                self, f"{name.title()}Service", cluster=cluster, task_definition=task,
                desired_count=background_desired_count.value_as_number, security_groups=[worker_sg],
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            )

        CfnOutput(self, "LoadBalancerDns", value=alb.load_balancer_dns_name)
        CfnOutput(self, "ApiDomain", value=domain_name.value_as_string)
        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "ClusterArn", value=cluster.cluster_arn)
        CfnOutput(self, "MigrationTaskArn", value=migration_task.task_definition_arn)
        CfnOutput(self, "PrivateSubnetIds", value=Fn.join(",", [subnet.subnet_id for subnet in vpc.private_subnets]))
        CfnOutput(self, "WorkerSecurityGroupId", value=worker_sg.security_group_id)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=app_client.user_pool_client_id)
        CfnOutput(self, "GitHubTokenSecretArn", value=github_secret.secret_arn)
        CfnOutput(self, "OpenAIKeySecretArn", value=openai_secret.secret_arn)


app = App()
environment = app.node.try_get_context("environment") or "staging"
teardown = str(app.node.try_get_context("teardown")).lower() == "true"
AgentsPlatformStack(
    app,
    f"AgentsPlatform-{environment}",
    production=environment == "production",
    teardown=teardown,
    env=Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1")),
    ),
)
app.synth()
