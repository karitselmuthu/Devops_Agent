from pathlib import Path
from types import SimpleNamespace

from agents_platform.app_aws import AppAwsDeployer, app_runner_template
from agents_platform.app_deployments import AppDeployment, AppPlanRequest, DeploymentStatus
from agents_platform.app_store import InMemoryAppStore


def plan() -> AppDeployment:
    return AppDeployment(
        request=AppPlanRequest(
            repository_url="https://github.com/acme/web-app", ref="main",
            name="web-app", environment="staging", port=8080,
        ),
        source_commit="a" * 40, created_by="admin-1", resources=[],
        ecr_repository="agents-app-staging-test", stack_name="AgentsApp-staging-test",
        image_tag="a" * 12,
    )


def test_template_uses_only_approved_image_and_port() -> None:
    template = app_runner_template(plan(), "123456789012.dkr.ecr.us-east-1.amazonaws.com/app:abc", "arn:aws:iam::123456789012:role/read-ecr")
    service = template["Resources"]["WebService"]
    source = service["Properties"]["SourceConfiguration"]
    assert source["ImageRepository"]["ImageConfiguration"]["Port"] == "8080"
    assert source["ImageRepository"]["ImageIdentifier"].endswith(":abc")
    assert source["AutoDeploymentsEnabled"] is False
    assert list(template["Resources"]) == ["WebService"]


def test_build_deploy_and_cleanup_with_injected_aws_clients(monkeypatch) -> None:
    deployment = plan()
    store = InMemoryAppStore()
    store.create(deployment)
    store.approve(deployment.id)
    calls = []

    class Ecr:
        exceptions = SimpleNamespace(RepositoryAlreadyExistsException=RuntimeError, RepositoryNotFoundException=RuntimeError)

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
            return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": "ServiceUrl", "OutputValue": "web.example.awsapprunner.com"}]}]}

        def delete_stack(self, **kwargs):
            calls.append("delete_stack")

        def get_waiter(self, name):
            return SimpleNamespace(wait=lambda **kwargs: calls.append("wait_delete"))

    deployer = AppAwsDeployer(
        region="us-east-1", source_bucket="source-bucket", build_project="build-project",
        access_role_arn="arn:aws:iam::123456789012:role/read-ecr",
        clients={"ecr": Ecr(), "s3": S3(), "codebuild": CodeBuild(), "cloudformation": CloudFormation()},
        poll_interval=0.01,
    )

    def fake_archive(_plan, workspace: Path) -> Path:
        archive = workspace / "source.zip"
        archive.write_bytes(b"zip")
        return archive

    monkeypatch.setattr(deployer, "_source_archive", fake_archive)
    deployer.deploy(deployment, store)
    assert store.get(deployment.id).status == DeploymentStatus.RUNNING
    assert store.get(deployment.id).service_url == "https://web.example.awsapprunner.com"
    assert calls[:5] == ["create_ecr", "upload_source", "start_build", "delete_source", "create_change_set"]
    assert "execute_change_set" in calls

    store.request_delete(deployment.id)
    deployer.delete(store.get(deployment.id), store)
    assert store.get(deployment.id).status == DeploymentStatus.DELETED
    assert "delete_stack" in calls and "delete_ecr" in calls
