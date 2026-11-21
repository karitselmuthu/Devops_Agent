from pathlib import Path
from types import SimpleNamespace

from agents_platform import app_aws
from agents_platform.app_aws import AppAwsDeployer, PlatformResources, ecs_app_template
from agents_platform.app_deployments import AppDeployment, AppPlanRequest, DeploymentStatus
from agents_platform.app_store import InMemoryAppStore


def plan() -> AppDeployment:
    return AppDeployment(
        request=AppPlanRequest(
            repository_url="https://github.com/acme/web-app", ref="main",
            name="web-app", environment="staging", port=8080, health_path="/health/ready",
        ),
        source_commit="a" * 40, created_by="admin-1", resources=[],
        ecr_repository="agents-app-staging-test", stack_name="AgentsApp-staging-test",
        image_tag="a" * 12,
    )


def platform() -> PlatformResources:
    return PlatformResources(
        region="us-east-1", cluster_arn="arn:aws:ecs:us-east-1:123456789012:cluster/shared",
        listener_arn="arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/shared/abc",
        vpc_id="vpc-0123", private_subnet_ids=("subnet-1", "subnet-2"),
        load_balancer_dns="internal-alb.example.com",
        load_balancer_security_group_id="sg-alb",
        task_execution_role_arn="arn:aws:iam::123456789012:role/execution",
        log_group_name="/aws/ecs/agents", origin_domain="internal-alb.example.com",
        vpc_origin_id="vo-test",
    )


def test_template_reuses_platform_and_routes_only_approved_image() -> None:
    deployment = plan()
    template = ecs_app_template(
        deployment, "123456789012.dkr.ecr.us-east-1.amazonaws.com/app:abc",
        platform(), listener_priority=1001, route_token="secret-route",
    )
    resources = template["Resources"]
    assert {item["Type"] for item in resources.values()} == {
        "AWS::EC2::SecurityGroup", "AWS::EC2::SecurityGroupEgress",
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        "AWS::ElasticLoadBalancingV2::ListenerRule", "AWS::ECS::TaskDefinition",
        "AWS::ECS::Service", "AWS::CloudFront::Distribution",
    }
    task = resources["AppTaskDefinition"]["Properties"]
    assert task["ContainerDefinitions"][0]["Image"].endswith(":abc")
    assert task["ContainerDefinitions"][0]["PortMappings"][0]["ContainerPort"] == 8080
    assert "TaskRoleArn" not in task
    assert resources["AppTargetGroup"]["Properties"]["HealthCheckPath"] == "/health/ready"
    egress = resources["AppAlbEgress"]["Properties"]
    assert egress["GroupId"] == platform().load_balancer_security_group_id
    assert egress["DestinationSecurityGroupId"] == {"Ref": "AppSecurityGroup"}
    assert egress["FromPort"] == egress["ToPort"] == 8080
    assert resources["AppService"]["DependsOn"] == ["AppRule", "AppAlbEgress"]
    assert resources["AppService"]["Properties"]["Cluster"] == platform().cluster_arn
    assert resources["AppService"]["Properties"]["DesiredCount"] == 1
    rule = resources["AppRule"]["Properties"]
    assert rule["Priority"] == 1001
    assert rule["Conditions"][0]["HttpHeaderConfig"]["Values"] == ["secret-route"]
    origin = resources["AppDistribution"]["Properties"]["DistributionConfig"]["Origins"][0]
    assert origin["VpcOriginConfig"]["VpcOriginId"] == "vo-test"
    assert origin["OriginCustomHeaders"][0]["HeaderValue"] == "secret-route"
    assert template["Outputs"]["ServiceUrl"]["Value"]["Fn::GetAtt"] == [
        "AppDistribution", "DomainName",
    ]


def test_production_uses_existing_https_load_balancer() -> None:
    deployment = plan()
    deployment.request.environment = "production"
    production = PlatformResources(
        **{**platform().__dict__, "origin_domain": "api.example.com", "vpc_origin_id": None}
    )
    origin = ecs_app_template(
        deployment, "image:tag", production, listener_priority=1002, route_token="secret",
    )["Resources"]["AppDistribution"]["Properties"]["DistributionConfig"]["Origins"][0]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert "VpcOriginConfig" not in origin


def test_staging_reads_shared_vpc_origin_from_portal_distribution() -> None:
    outputs = {
        "ClusterArn": platform().cluster_arn,
        "AppListenerArn": platform().listener_arn,
        "AppVpcId": platform().vpc_id,
        "PrivateSubnetIds": "subnet-1,subnet-2",
        "LoadBalancerDns": platform().load_balancer_dns,
        "AppLoadBalancerSecurityGroupId": platform().load_balancer_security_group_id,
        "AppTaskExecutionRoleArn": platform().task_execution_role_arn,
        "AppLogGroupName": platform().log_group_name,
        "ApiDomain": "portal.example.cloudfront.net",
        "AppPortalDistributionId": "E12345",
    }

    class CloudFormation:
        def describe_stacks(self, **kwargs):
            assert kwargs["StackName"] == "AgentsPlatform-staging"
            return {"Stacks": [{"Outputs": [
                {"OutputKey": key, "OutputValue": value} for key, value in outputs.items()
            ]}]}

    class CloudFront:
        def get_distribution_config(self, **kwargs):
            assert kwargs["Id"] == "E12345"
            return {"DistributionConfig": {"Origins": {"Items": [
                {"VpcOriginConfig": {"VpcOriginId": "vo-shared"}},
            ]}}}

    deployer = AppAwsDeployer(
        region="us-east-1", source_bucket="source-bucket", build_project="build-project",
        platform_stack="AgentsPlatform-staging",
        clients={"cloudformation": CloudFormation(), "cloudfront": CloudFront()},
    )
    settings = deployer._platform_resources(plan())
    assert settings.vpc_origin_id == "vo-shared"
    assert settings.origin_domain == platform().load_balancer_dns


def test_build_deploy_and_cleanup_with_injected_aws_clients(monkeypatch) -> None:
    deployment = plan()
    store = InMemoryAppStore()
    store.create(deployment)
    store.approve(deployment.id)
    calls = []

    class Ecr:
        exceptions = SimpleNamespace(
            RepositoryAlreadyExistsException=RuntimeError,
            RepositoryNotFoundException=RuntimeError,
        )

        def create_repository(self, **kwargs):
            calls.append("create_ecr")
            return {"repository": {"repositoryUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/app"}}

        def delete_repository(self, **kwargs):
            calls.append("delete_ecr")

    class S3:
        def upload_file(self, *args, **kwargs):
            calls.append("upload_source")

        def delete_object(self, **kwargs):
            calls.append("delete_source")

    class CodeBuild:
        def start_build(self, **kwargs):
            calls.append("start_build")
            assert kwargs["sourceTypeOverride"] == "S3"
            env = {item["name"]: item["value"] for item in kwargs["environmentVariablesOverride"]}
            assert env["DOCKERFILE_PATH"] == deployment.request.dockerfile_path
            return {"build": {"id": "build-1"}}

        def batch_get_builds(self, **kwargs):
            return {"builds": [{"buildStatus": "SUCCEEDED"}]}

    class CloudFormation:
        def create_change_set(self, **kwargs):
            calls.append("create_change_set")
            return {"Id": "change-set-1"}

        def describe_change_set(self, **kwargs):
            return {"Status": "CREATE_COMPLETE"}

        def execute_change_set(self, **kwargs):
            calls.append("execute_change_set")

        def describe_stacks(self, **kwargs):
            return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": [
                {"OutputKey": "ServiceUrl", "OutputValue": "app.example.cloudfront.net"},
            ]}]}

        def delete_stack(self, **kwargs):
            calls.append("delete_stack")

        def get_waiter(self, name):
            return SimpleNamespace(wait=lambda **kwargs: calls.append("wait_delete"))

    deployer = AppAwsDeployer(
        region="us-east-1", source_bucket="source-bucket", build_project="build-project",
        platform_stack="AgentsPlatform-staging",
        clients={"ecr": Ecr(), "s3": S3(), "codebuild": CodeBuild(),
                 "cloudformation": CloudFormation()},
        poll_interval=0.01,
    )

    def fake_archive(_plan, workspace: Path) -> Path:
        archive = workspace / "source.zip"
        archive.write_bytes(b"zip")
        return archive

    monkeypatch.setattr(deployer, "_source_archive", fake_archive)
    monkeypatch.setattr(deployer, "_platform_resources", lambda _: platform())
    monkeypatch.setattr(deployer, "_listener_priority", lambda _: 1001)
    monkeypatch.setattr(app_aws.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    deployer.deploy(deployment, store)
    assert store.get(deployment.id).status == DeploymentStatus.RUNNING
    assert store.get(deployment.id).service_url == "https://app.example.cloudfront.net"
    assert calls[:5] == ["create_ecr", "upload_source", "start_build", "delete_source", "create_change_set"]
    assert "execute_change_set" in calls

    store.set_status(deployment.id, DeploymentStatus.FAILED, error="previous stack failure")
    store.request_delete(deployment.id)
    deployer.delete(store.get(deployment.id), store)
    assert store.get(deployment.id).status == DeploymentStatus.DELETED
    assert store.get(deployment.id).error is None
    assert "delete_stack" in calls and "delete_ecr" in calls
