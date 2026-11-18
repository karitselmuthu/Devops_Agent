"""Build an approved GitHub checkout and operate its App Runner stack."""

import json
import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import boto3
from botocore.exceptions import ClientError

from agents_platform.app_deployments import AppDeployment, DeploymentStatus
from agents_platform.source import checkout_source


MAX_SOURCE_ARCHIVE_BYTES = 50 * 1024 * 1024


def app_runner_template(plan: AppDeployment, image_uri: str, access_role_arn: str) -> dict:
    suffix = plan.id.hex[:8]
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "One approved Agents Platform web application",
        "Resources": {
            "WebService": {
                "Type": "AWS::AppRunner::Service",
                "Properties": {
                    "ServiceName": f"{plan.request.name[:30]}-{suffix}",
                    "SourceConfiguration": {
                        "AuthenticationConfiguration": {"AccessRoleArn": access_role_arn},
                        "AutoDeploymentsEnabled": False,
                        "ImageRepository": {
                            "ImageIdentifier": image_uri,
                            "ImageRepositoryType": "ECR",
                            "ImageConfiguration": {"Port": str(plan.request.port)},
                        },
                    },
                    "InstanceConfiguration": {"Cpu": "1 vCPU", "Memory": "2 GB"},
                },
            }
        },
        "Outputs": {
            "ServiceUrl": {"Value": {"Fn::GetAtt": ["WebService", "ServiceUrl"]}}
        },
    }


class AppAwsDeployer:
    def __init__(self, *, region: str, source_bucket: str, build_project: str,
                 access_role_arn: str, clients: dict[str, object] | None = None,
                 poll_interval: float = 10, timeout_seconds: float = 2400) -> None:
        if not all((region, source_bucket, build_project, access_role_arn)):
            raise ValueError("App deployment AWS settings are required")
        self.region = region
        self.source_bucket = source_bucket
        self.build_project = build_project
        self.access_role_arn = access_role_arn
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds
        session = boto3.Session(region_name=region) if clients is None else None
        self.clients = clients or {
            name: session.client(name) for name in ("ecr", "s3", "codebuild", "cloudformation")
        }

    @classmethod
    def from_environment(cls) -> "AppAwsDeployer":
        return cls(
            region=os.environ["AWS_REGION"],
            source_bucket=os.environ["APP_SOURCE_BUCKET"],
            build_project=os.environ["APP_CODEBUILD_PROJECT"],
            access_role_arn=os.environ["APP_RUNNER_ACCESS_ROLE_ARN"],
        )

    @staticmethod
    def _source_key(plan: AppDeployment) -> str:
        return f"app-sources/{plan.id}/source.zip"

    def _repository_uri(self, plan: AppDeployment) -> str:
        ecr = self.clients["ecr"]
        try:
            response = ecr.create_repository(
                repositoryName=plan.ecr_repository,
                imageTagMutability="IMMUTABLE",
                imageScanningConfiguration={"scanOnPush": True},
                encryptionConfiguration={"encryptionType": "AES256"},
                tags=[{"Key": "ManagedBy", "Value": "AgentsPlatform"},
                      {"Key": "DeploymentId", "Value": str(plan.id)}],
            )
            return response["repository"]["repositoryUri"]
        except ecr.exceptions.RepositoryAlreadyExistsException:
            return ecr.describe_repositories(repositoryNames=[plan.ecr_repository])["repositories"][0]["repositoryUri"]

    def _source_archive(self, plan: AppDeployment, workspace: Path) -> Path:
        repository = workspace / "source"
        checkout_source(plan.request.source_target(), repository)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if commit != plan.source_commit:
            raise RuntimeError("Repository branch changed after review. Create a new deployment plan.")
        if not (repository / "Dockerfile").is_file():
            raise RuntimeError("The approved Dockerfile is missing")
        archive = workspace / "source.zip"
        subprocess.run(
            ["git", "archive", "--format=zip", "--output", str(archive), "HEAD"],
            cwd=repository, check=True, capture_output=True, text=True, timeout=60,
        )
        if archive.stat().st_size > MAX_SOURCE_ARCHIVE_BYTES:
            raise RuntimeError("Source archive exceeds the 50 MiB deployment limit")
        return archive

    def _wait_build(self, build_id: str) -> None:
        codebuild = self.clients["codebuild"]
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            builds = codebuild.batch_get_builds(ids=[build_id])["builds"]
            if not builds:
                raise RuntimeError("CodeBuild did not return the build")
            status = builds[0]["buildStatus"]
            if status == "SUCCEEDED":
                return
            if status in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
                raise RuntimeError(f"Container build {status.lower()}; inspect CodeBuild build {build_id}")
            time.sleep(self.poll_interval)
        raise TimeoutError("Container build did not finish within the deployment timeout")

    def _wait_change_set(self, plan: AppDeployment, change_set_id: str) -> None:
        cloudformation = self.clients["cloudformation"]
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            response = cloudformation.describe_change_set(
                StackName=plan.stack_name, ChangeSetName=change_set_id
            )
            if response["Status"] == "CREATE_COMPLETE":
                return
            if response["Status"] == "FAILED":
                raise RuntimeError("Application change set failed; inspect CloudFormation events")
            time.sleep(self.poll_interval)
        raise TimeoutError("Application change set did not finish")

    def _wait_stack(self, plan: AppDeployment) -> str:
        cloudformation = self.clients["cloudformation"]
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            stack = cloudformation.describe_stacks(StackName=plan.stack_name)["Stacks"][0]
            status = stack["StackStatus"]
            if status == "CREATE_COMPLETE":
                outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
                return f"https://{outputs['ServiceUrl']}"
            if "ROLLBACK" in status or status.endswith("FAILED"):
                raise RuntimeError(f"Application stack failed: {status}")
            time.sleep(self.poll_interval)
        raise TimeoutError("Application stack did not finish")

    def deploy(self, plan: AppDeployment, store) -> None:
        store.set_status(plan.id, DeploymentStatus.BUILDING, error=None)
        repository_uri = self._repository_uri(plan)
        image_uri = f"{repository_uri}:{plan.image_tag}"
        with TemporaryDirectory() as temporary:
            archive = self._source_archive(plan, Path(temporary))
            self.clients["s3"].upload_file(
                str(archive), self.source_bucket, self._source_key(plan),
                ExtraArgs={"ServerSideEncryption": "AES256"},
            )
        try:
            build = self.clients["codebuild"].start_build(
                projectName=self.build_project,
                sourceTypeOverride="S3",
                sourceLocationOverride=f"{self.source_bucket}/{self._source_key(plan)}",
                environmentVariablesOverride=[
                    {"name": "IMAGE_URI", "value": image_uri, "type": "PLAINTEXT"},
                ],
                idempotencyToken=str(plan.id),
            )
            build_id = build["build"]["id"]
            store.set_status(plan.id, DeploymentStatus.BUILDING, build_id=build_id)
            self._wait_build(build_id)
        finally:
            self.clients["s3"].delete_object(Bucket=self.source_bucket, Key=self._source_key(plan))

        store.set_status(plan.id, DeploymentStatus.DEPLOYING)
        change_set = self.clients["cloudformation"].create_change_set(
            StackName=plan.stack_name,
            ChangeSetName=f"agents-app-{plan.id.hex[:12]}",
            ChangeSetType="CREATE",
            TemplateBody=json.dumps(app_runner_template(plan, image_uri, self.access_role_arn)),
            Tags=[{"Key": "ManagedBy", "Value": "AgentsPlatform"},
                  {"Key": "DeploymentId", "Value": str(plan.id)}],
        )
        self._wait_change_set(plan, change_set["Id"])
        self.clients["cloudformation"].execute_change_set(
            StackName=plan.stack_name, ChangeSetName=change_set["Id"]
        )
        url = self._wait_stack(plan)
        store.set_status(plan.id, DeploymentStatus.RUNNING, service_url=url)

    def delete(self, plan: AppDeployment, store) -> None:
        cloudformation = self.clients["cloudformation"]
        try:
            cloudformation.describe_stacks(StackName=plan.stack_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ValidationError":
                raise
        else:
            cloudformation.delete_stack(StackName=plan.stack_name)
            cloudformation.get_waiter("stack_delete_complete").wait(StackName=plan.stack_name)
        ecr = self.clients["ecr"]
        try:
            ecr.delete_repository(repositoryName=plan.ecr_repository, force=True)
        except ecr.exceptions.RepositoryNotFoundException:
            pass
        self.clients["s3"].delete_object(Bucket=self.source_bucket, Key=self._source_key(plan))
        store.set_status(plan.id, DeploymentStatus.DELETED, service_url=None)
