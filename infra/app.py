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
    aws_s3 as s3,
    aws_codebuild as codebuild,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as cloudfront_origins,
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

        if production:
            certificate_arn = CfnParameter(self, "CertificateArn", type="String")
            domain_name = CfnParameter(self, "DomainName", type="String")
            hosted_zone_id = CfnParameter(self, "HostedZoneId", type="String")
        else:
            cloudfront_prefix_list_id = CfnParameter(
                self, "CloudFrontPrefixListId", type="String", allowed_pattern="^pl-[0-9a-f]+$"
            )
            staging_client_id = CfnParameter(
                self, "StagingCognitoClientId", type="String", default="bootstrap"
            )
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
        app_queue = sqs.Queue(
            self, "AppDeployQueue", fifo=True,
            encryption=sqs.QueueEncryption.KMS, encryption_master_key=queue_key,
            visibility_timeout=Duration.seconds(120),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dead_letter_queue, max_receive_count=5),
        )

        app_source_bucket = s3.Bucket(
            self, "AppSourceBucket", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(1))],
            removal_policy=persistent_policy,
            auto_delete_objects=teardown,
        )
        app_build = codebuild.Project(
            self, "AppContainerBuild",
            source=codebuild.Source.s3(
                bucket=app_source_bucket, path="app-sources/placeholder.zip"
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,
            ),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "pre_build": {"commands": [
                        'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "${IMAGE_URI%%/*}"',
                    ]},
                    "build": {"commands": ['docker build --pull -t "$IMAGE_URI" .']},
                    "post_build": {"commands": ['docker push "$IMAGE_URI"']},
                },
            }),
            timeout=Duration.minutes(30),
        )
        app_source_bucket.grant_read(app_build.role)
        app_build.role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:GetAuthorizationToken"], resources=["*"],
        ))
        app_build.role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
                     "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
                     "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
            resources=[f"arn:{self.partition}:ecr:{self.region}:{self.account}:repository/agents-app-*"],
        ))
        # Keep the previous worker image bootable during the first deployment phase.
        app_runner_access_role = iam.Role(
            self, "AppRunnerEcrAccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSAppRunnerServicePolicyForECRAccess"
            )],
        )

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
        def add_app_client(url: str) -> cognito.UserPoolClient:
            return pool.add_client(
                "ApiClient",
                generate_secret=False,
                auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[cognito.OAuthScope.OPENID],
                    callback_urls=[url],
                    logout_urls=[url],
                ),
            )

        if production:
            app_client = add_app_client(f"https://{domain_name.value_as_string}/")
        login_domain = pool.add_domain(
            "LoginDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"agents-{construct_id.lower()}-{self.account}-{self.region}"
            ),
        )
        for group in ("agents-readers", "agents-operators", "agents-admins"):
            cognito.CfnUserPoolGroup(self, group.replace("-", "").title(),
                user_pool_id=pool.user_pool_id, group_name=group)

        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)
        common_env = {
            "AGENTS_MODE": "aws",
            "AGENTS_ENVIRONMENT": construct_id.removeprefix("AgentsPlatform-"),
            "AWS_REGION": self.region,
            "DB_HOST": database.db_instance_endpoint_address,
            "DB_PORT": database.db_instance_endpoint_port,
            "DB_NAME": "agents",
            "DB_USER": "agents",
            "AGENTS_ALLOWED_REPOS": allowed_repos.value_as_string,
            "AGENTS_ALLOWED_PIPELINES": allowed_pipelines.value_as_string,
            "AGENTS_ALLOWED_STACKS": allowed_stacks.value_as_string,
            **{f"SQS_{name}_URL": queue.queue_url for name, queue in queues.items()},
            "SQS_APP_DEPLOY_URL": app_queue.queue_url,
        }
        db_secret = {"DB_PASSWORD": ecs.Secret.from_secrets_manager(database.secret, "password")}
        image_log = logs.LogGroup(
            self, "ApplicationLogs", retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=persistent_policy,
        )
        app_execution_role = iam.Role(
            self, "AppImageExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        app_execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:GetAuthorizationToken"], resources=["*"],
        ))
        app_execution_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
                     "ecr:BatchGetImage"],
            resources=[f"arn:{self.partition}:ecr:{self.region}:{self.account}:repository/agents-app-*"],
        ))
        image_log.grant_write(app_execution_role)

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
            scratch_init = task.add_container(
                f"{role}ScratchInit",
                image=image,
                entry_point=["/bin/sh", "-c"],
                command=["chmod 1777 /tmp"],
                user="0",
                essential=False,
                readonly_root_filesystem=True,
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=f"{role.lower()}-scratch", log_group=image_log
                ),
            )
            scratch_init.add_mount_points(ecs.MountPoint(
                container_path="/tmp", source_volume="scratch", read_only=False
            ))
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
            container.add_container_dependencies(ecs.ContainerDependency(
                container=scratch_init,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            ))
            if role == "Api":
                container.add_port_mappings(ecs.PortMapping(container_port=8000))
                container.add_environment("COGNITO_USER_POOL_ID", pool.user_pool_id)
                container.add_environment(
                    "COGNITO_APP_CLIENT_ID",
                    app_client.user_pool_client_id if production else staging_client_id.value_as_string,
                )
                container.add_environment("COGNITO_DOMAIN", login_domain.base_url())
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
        alb = elbv2.ApplicationLoadBalancer(
            self, "LoadBalancer", vpc=vpc, internet_facing=production,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC if production else ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )
        if production:
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
            api_domain = domain_name.value_as_string
        else:
            listener = alb.add_listener("Http", port=80, open=False)
            cloudfront_origin_prefix = ec2.PrefixList.from_prefix_list_id(
                self, "CloudFrontOriginPrefix", cloudfront_prefix_list_id.value_as_string
            )
            alb.connections.allow_from(cloudfront_origin_prefix, ec2.Port.tcp(80))
            distribution = cloudfront.Distribution(
                self, "StagingDistribution",
                default_behavior=cloudfront.BehaviorOptions(
                    origin=cloudfront_origins.VpcOrigin.with_application_load_balancer(
                        alb, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY
                    ),
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                ),
            )
            api_domain = distribution.distribution_domain_name
            app_client = add_app_client(f"https://{api_domain}/")
        listener.add_targets(
            "ApiTargets", port=8000,
            targets=[api_service.load_balancer_target(container_name="Api", container_port=8000)],
            health_check=elbv2.HealthCheck(path="/health"),
        )
        api_sg.add_ingress_rule(alb.connections.security_groups[0], ec2.Port.tcp(8000))

        dispatcher_task = task_definition("Dispatcher", ["python", "-m", "agents_platform.runtime", "dispatcher"])
        for name in AGENTS:
            queues[name].grant_send_messages(dispatcher_task.task_role)
        app_queue.grant_send_messages(dispatcher_task.task_role)
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

        app_worker_task = task_definition(
            "AppDeployWorker", ["python", "-m", "agents_platform.app_runtime"],
            extra_env={
                "APP_SOURCE_BUCKET": app_source_bucket.bucket_name,
                "APP_CODEBUILD_PROJECT": app_build.project_name,
                "APP_RUNNER_ACCESS_ROLE_ARN": app_runner_access_role.role_arn,
                "APP_PLATFORM_STACK": self.stack_name,
            },
            extra_secrets={"GITHUB_TOKEN": ecs.Secret.from_secrets_manager(github_secret)},
        )
        app_queue.grant_consume_messages(app_worker_task.task_role)
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:DeleteObject"],
            resources=[app_source_bucket.arn_for_objects("app-sources/*")],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
            resources=[app_build.project_arn],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:DeleteRepository",
                     "ecr:TagResource"],
            resources=[f"arn:{self.partition}:ecr:{self.region}:{self.account}:repository/agents-app-*"],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
                     "cloudformation:ExecuteChangeSet", "cloudformation:DescribeStacks",
                     "cloudformation:DeleteStack"],
            resources=[
                f"arn:{self.partition}:cloudformation:{self.region}:{self.account}:stack/AgentsApp-*/*",
                f"arn:{self.partition}:cloudformation:{self.region}:{self.account}:changeSet/agents-app-*/*",
            ],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:DescribeStacks"], resources=[self.stack_id],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["apprunner:CreateService", "apprunner:DescribeService", "apprunner:DeleteService",
                     "apprunner:TagResource"], resources=["*"],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"], resources=[app_runner_access_role.role_arn],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:CreateServiceLinkedRole"], resources=["*"],
            conditions={"StringEquals": {"iam:AWSServiceName": "apprunner.amazonaws.com"}},
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["elasticloadbalancing:DescribeRules"], resources=["*"],
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"], resources=[app_execution_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
        ))
        app_worker_task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition",
                     "ecs:DescribeTaskDefinition", "ecs:CreateService", "ecs:DeleteService",
                     "ecs:DescribeServices", "ecs:UpdateService", "ecs:TagResource",
                     "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:DescribeSecurityGroups",
                     "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress",
                     "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress",
                     "ec2:CreateTags", "elasticloadbalancing:CreateTargetGroup",
                     "elasticloadbalancing:DeleteTargetGroup", "elasticloadbalancing:DescribeTargetGroups",
                     "elasticloadbalancing:CreateRule", "elasticloadbalancing:DeleteRule",
                     "elasticloadbalancing:DescribeListeners", "elasticloadbalancing:DescribeTargetHealth",
                     "elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags",
                     "cloudfront:CreateDistribution", "cloudfront:CreateDistributionWithTags",
                     "cloudfront:GetDistribution", "cloudfront:ListTagsForResource",
                     "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution",
                     "cloudfront:DeleteDistribution", "cloudfront:TagResource",
                     "cloudfront:UntagResource"],
            resources=["*"],
        ))
        ecs.FargateService(
            self, "AppDeployService", cluster=cluster, task_definition=app_worker_task,
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
        CfnOutput(self, "ApiDomain", value=api_domain)
        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "ClusterArn", value=cluster.cluster_arn)
        CfnOutput(self, "MigrationTaskArn", value=migration_task.task_definition_arn)
        CfnOutput(self, "PrivateSubnetIds", value=Fn.join(",", [subnet.subnet_id for subnet in vpc.private_subnets]))
        CfnOutput(self, "WorkerSecurityGroupId", value=worker_sg.security_group_id)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=app_client.user_pool_client_id)
        CfnOutput(self, "CognitoLoginUrl", value=login_domain.base_url())
        CfnOutput(self, "GitHubTokenSecretArn", value=github_secret.secret_arn)
        CfnOutput(self, "OpenAIKeySecretArn", value=openai_secret.secret_arn)
        CfnOutput(self, "AppSourceBucketName", value=app_source_bucket.bucket_name)
        CfnOutput(self, "AppCodeBuildProjectName", value=app_build.project_name)
        CfnOutput(self, "AppListenerArn", value=listener.listener_arn)
        CfnOutput(self, "AppVpcId", value=vpc.vpc_id)
        CfnOutput(self, "AppLoadBalancerArn", value=alb.load_balancer_arn)
        CfnOutput(self, "AppLoadBalancerSecurityGroupId", value=alb.connections.security_groups[0].security_group_id)
        CfnOutput(self, "AppTaskExecutionRoleArn", value=app_execution_role.role_arn)
        CfnOutput(self, "AppLogGroupName", value=image_log.log_group_name)
        CfnOutput(self, "AppRunnerAccessRoleArn", value=app_runner_access_role.role_arn)
        if not production:
            CfnOutput(self, "AppPortalDistributionId", value=distribution.distribution_id)


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
