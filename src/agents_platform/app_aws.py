"""Build approved images and deploy web apps into the shared ECS cluster."""

import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import boto3
import httpx
from botocore.exceptions import ClientError

from agents_platform.app_deployments import AppDeployment, DeploymentStatus
from agents_platform.source import checkout_source


MAX_SOURCE_ARCHIVE_BYTES = 50 * 1024 * 1024
CACHE_DISABLED_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST_POLICY_ID = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


@dataclass(frozen=True)
class PlatformResources:
    region: str
    cluster_arn: str
    listener_arn: str
    vpc_id: str
    private_subnet_ids: tuple[str, ...]
    load_balancer_dns: str
    load_balancer_security_group_id: str
    task_execution_role_arn: str
    log_group_name: str
    origin_domain: str
    vpc_origin_id: str | None


def ecs_app_template(plan: AppDeployment, image_uri: str, platform: PlatformResources,
                     *, listener_priority: int, route_token: str) -> dict:
    """Create app-owned resources while reusing the platform cluster, ALB and VPC."""
    service_name = f"agents-app-{plan.id.hex[:12]}"
    port = plan.request.port
    origin = {
        "Id": "SharedPlatformAlb",
        "DomainName": platform.origin_domain,
        "OriginCustomHeaders": [{"HeaderName": "X-Agents-App-Route", "HeaderValue": route_token}],
    }
    if platform.vpc_origin_id:
        origin["VpcOriginConfig"] = {"VpcOriginId": platform.vpc_origin_id}
    else:
        origin["CustomOriginConfig"] = {
            "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
            "OriginSSLProtocols": ["TLSv1.2"],
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "One approved Agents Platform web application on shared ECS and ALB",
        "Resources": {
            "AppSecurityGroup": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": f"Ingress from shared ALB for {service_name}",
                    "VpcId": platform.vpc_id,
                    "SecurityGroupIngress": [{
                        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                        "SourceSecurityGroupId": platform.load_balancer_security_group_id,
                    }],
                    "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}],
                },
            },
            "AppAlbEgress": {
                "Type": "AWS::EC2::SecurityGroupEgress",
                "Properties": {
                    "GroupId": platform.load_balancer_security_group_id,
                    "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                    "DestinationSecurityGroupId": {"Ref": "AppSecurityGroup"},
                },
            },
            "AppTargetGroup": {
                "Type": "AWS::ElasticLoadBalancingV2::TargetGroup",
                "Properties": {
                    "Name": service_name, "VpcId": platform.vpc_id,
                    "Protocol": "HTTP", "Port": port, "TargetType": "ip",
                    "HealthCheckPath": plan.request.health_path,
                    "HealthCheckProtocol": "HTTP", "Matcher": {"HttpCode": "200-399"},
                },
            },
            "AppRule": {
                "Type": "AWS::ElasticLoadBalancingV2::ListenerRule",
                "Properties": {
                    "ListenerArn": platform.listener_arn, "Priority": listener_priority,
                    "Conditions": [{
                        "Field": "http-header",
                        "HttpHeaderConfig": {
                            "HttpHeaderName": "X-Agents-App-Route", "Values": [route_token],
                        },
                    }],
                    "Actions": [{"Type": "forward", "TargetGroupArn": {"Ref": "AppTargetGroup"}}],
                },
            },
            "AppTaskDefinition": {
                "Type": "AWS::ECS::TaskDefinition",
                "Properties": {
                    "Family": service_name, "Cpu": "256", "Memory": "512",
                    "NetworkMode": "awsvpc", "RequiresCompatibilities": ["FARGATE"],
                    "ExecutionRoleArn": platform.task_execution_role_arn,
                    "ContainerDefinitions": [{
                        "Name": "Web", "Image": image_uri, "Essential": True,
                        "PortMappings": [{"ContainerPort": port, "Protocol": "tcp"}],
                        "LogConfiguration": {
                            "LogDriver": "awslogs",
                            "Options": {
                                "awslogs-group": platform.log_group_name,
                                "awslogs-region": platform.region,
                                "awslogs-stream-prefix": service_name,
                            },
                        },
                    }],
                },
            },
            "AppService": {
                "Type": "AWS::ECS::Service", "DependsOn": ["AppRule", "AppAlbEgress"],
                "Properties": {
                    "ServiceName": service_name, "Cluster": platform.cluster_arn,
                    "TaskDefinition": {"Ref": "AppTaskDefinition"},
                    "LaunchType": "FARGATE", "DesiredCount": 1,
                    "HealthCheckGracePeriodSeconds": 90,
                    "DeploymentConfiguration": {
                        "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
                    },
                    "LoadBalancers": [{
                        "ContainerName": "Web", "ContainerPort": port,
                        "TargetGroupArn": {"Ref": "AppTargetGroup"},
                    }],
                    "NetworkConfiguration": {
                        "AwsvpcConfiguration": {
                            "AssignPublicIp": "DISABLED",
                            "SecurityGroups": [{"Ref": "AppSecurityGroup"}],
                            "Subnets": list(platform.private_subnet_ids),
                        },
                    },
                },
            },
            "AppDistribution": {
                "Type": "AWS::CloudFront::Distribution", "DependsOn": ["AppService"],
                "Properties": {
                    "DistributionConfig": {
                        "Enabled": True, "HttpVersion": "http2",
                        "Origins": [origin],
                        "DefaultCacheBehavior": {
                            "TargetOriginId": "SharedPlatformAlb",
                            "ViewerProtocolPolicy": "redirect-to-https",
                            "AllowedMethods": [
                                "GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE",
                            ],
                            "CachePolicyId": CACHE_DISABLED_POLICY_ID,
                            "OriginRequestPolicyId": ALL_VIEWER_EXCEPT_HOST_POLICY_ID,
                        },
                        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
                    },
                },
            },
        },
        "Outputs": {
            "ServiceUrl": {"Value": {"Fn::GetAtt": ["AppDistribution", "DomainName"]}},
        },
    }


class AppAwsDeployer:
    def __init__(self, *, region: str, source_bucket: str, build_project: str,
                 platform_stack: str, clients: dict[str, object] | None = None,
                 poll_interval: float = 10, timeout_seconds: float = 2400) -> None:
        if not all((region, source_bucket, build_project, platform_stack)):
            raise ValueError("App deployment AWS settings are required")
        self.region = region
        self.source_bucket = source_bucket
        self.build_project = build_project
        self.platform_stack = platform_stack
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds
        session = boto3.Session(region_name=region) if clients is None else None
        self.clients = clients or {
            name: session.client(name)
            for name in ("ecr", "s3", "codebuild", "cloudformation", "elbv2", "cloudfront")
        }

    @classmethod
    def from_environment(cls) -> "AppAwsDeployer":
        return cls(
            region=os.environ["AWS_REGION"],
            source_bucket=os.environ["APP_SOURCE_BUCKET"],
            build_project=os.environ["APP_CODEBUILD_PROJECT"],
            platform_stack=os.environ["APP_PLATFORM_STACK"],
        )

    @staticmethod
    def _source_key(plan: AppDeployment) -> str:
        return f"app-sources/{plan.id}/source.zip"

    def _platform_resources(self, plan: AppDeployment) -> PlatformResources:
        if self.platform_stack != f"AgentsPlatform-{plan.request.environment}":
            raise ValueError("App environment does not match the platform stack")
        stack = self.clients["cloudformation"].describe_stacks(
            StackName=self.platform_stack
        )["Stacks"][0]
        outputs = {item["OutputKey"]: item["OutputValue"] for item in stack["Outputs"]}
        vpc_origin_id = None
        origin_domain = outputs["ApiDomain"]
        if plan.request.environment == "staging":
            distribution = self.clients["cloudfront"].get_distribution_config(
                Id=outputs["AppPortalDistributionId"]
            )["DistributionConfig"]
            origins = distribution["Origins"]["Items"]
            ids = [item["VpcOriginConfig"]["VpcOriginId"] for item in origins
                   if item.get("VpcOriginConfig")]
            if len(ids) != 1:
                raise RuntimeError("Expected one platform CloudFront VPC origin")
            vpc_origin_id = ids[0]
            origin_domain = outputs["LoadBalancerDns"]
        return PlatformResources(
            region=self.region,
            cluster_arn=outputs["ClusterArn"],
            listener_arn=outputs["AppListenerArn"],
            vpc_id=outputs["AppVpcId"],
            private_subnet_ids=tuple(outputs["PrivateSubnetIds"].split(",")),
            load_balancer_dns=outputs["LoadBalancerDns"],
            load_balancer_security_group_id=outputs["AppLoadBalancerSecurityGroupId"],
            task_execution_role_arn=outputs["AppTaskExecutionRoleArn"],
            log_group_name=outputs["AppLogGroupName"],
            origin_domain=origin_domain,
            vpc_origin_id=vpc_origin_id,
        )

    def _listener_priority(self, listener_arn: str) -> int:
        rules = [rule for page in self.clients["elbv2"].get_paginator("describe_rules").paginate(
            ListenerArn=listener_arn
        ) for rule in page["Rules"]]
        used = {int(rule["Priority"]) for rule in rules if rule["Priority"].isdigit()}
        for priority in range(1000, 50001):
            if priority not in used:
                return priority
        raise RuntimeError("No listener rule priority is available")

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
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            builds = self.clients["codebuild"].batch_get_builds(ids=[build_id])["builds"]
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
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            response = self.clients["cloudformation"].describe_change_set(
                StackName=plan.stack_name, ChangeSetName=change_set_id
            )
            if response["Status"] == "CREATE_COMPLETE":
                return
            if response["Status"] == "FAILED":
                raise RuntimeError("Application change set failed; inspect CloudFormation events")
            time.sleep(self.poll_interval)
        raise TimeoutError("Application change set did not finish")

    def _wait_stack(self, plan: AppDeployment) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            stack = self.clients["cloudformation"].describe_stacks(StackName=plan.stack_name)["Stacks"][0]
            status = stack["StackStatus"]
            if status == "CREATE_COMPLETE":
                outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
                return f"https://{outputs['ServiceUrl']}"
            if "ROLLBACK" in status or status.endswith("FAILED"):
                raise RuntimeError(f"Application stack failed: {status}")
            time.sleep(self.poll_interval)
        raise TimeoutError("Application stack did not finish")

    def _wait_health(self, url: str, path: str) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}{path}", timeout=10).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(self.poll_interval)
        raise RuntimeError(f"Application health check failed at {path}")

    def deploy(self, plan: AppDeployment, store) -> None:
        platform = self._platform_resources(plan)
        priority = self._listener_priority(platform.listener_arn)
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
            TemplateBody=json.dumps(ecs_app_template(
                plan, image_uri, platform, listener_priority=priority,
                route_token=secrets.token_hex(16),
            )),
            Tags=[{"Key": "ManagedBy", "Value": "AgentsPlatform"},
                  {"Key": "DeploymentId", "Value": str(plan.id)}],
        )
        self._wait_change_set(plan, change_set["Id"])
        self.clients["cloudformation"].execute_change_set(
            StackName=plan.stack_name, ChangeSetName=change_set["Id"]
        )
        url = self._wait_stack(plan)
        store.set_status(plan.id, DeploymentStatus.DEPLOYING, service_url=url)
        self._wait_health(url, plan.request.health_path)
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
        store.set_status(plan.id, DeploymentStatus.DELETED, service_url=None, error=None)
