import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents_platform import app_deployments
from agents_platform.app_aws import AppAwsDeployer
from agents_platform.app_deployments import AppPlanRequest, DeploymentKind, DeploymentStatus
from agents_platform.sam_blueprint import load_sam_template, packaged_sam_template


SAM_TEMPLATE = '''
AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Resources:
  MemoryTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
  Api:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: build/
      Handler: handler.handler
      Runtime: python3.12
      Timeout: 180
      MemorySize: 1024
      FunctionUrlConfig:
        AuthType: AWS_IAM
      Policies:
        - DynamoDBCrudPolicy: {TableName: !Ref MemoryTable}
        - Statement:
            - Effect: Allow
              Action: [bedrock:InvokeModel]
              Resource: !Sub "arn:aws:bedrock:${AWS::Region}::foundation-model/amazon.nova-lite-v1:0"
Outputs:
  Url:
    Value: !GetAtt ApiUrl.FunctionUrl
'''


def sam_plan(monkeypatch, template=SAM_TEMPLATE):
    def checkout(_source, destination):
        destination.mkdir()
        (destination / "template.yaml").write_text(template)
        (destination / "build.sh").write_text("#!/bin/sh\nmkdir -p build\n")

    monkeypatch.setattr(app_deployments, "checkout_source", checkout)
    monkeypatch.setattr(
        app_deployments.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "a" * 40 + "\n", ""),
    )
    request = AppPlanRequest(
        repository_url="https://github.com/acme/multi_agent_rag", ref="main",
        name="multi-agent-rag", environment="staging",
    )
    return app_deployments.create_app_plan(request, "operator-1")


def test_sam_repository_creates_lambda_plan_without_dockerfile_or_port(monkeypatch) -> None:
    plan = sam_plan(monkeypatch)
    assert plan.deployment_kind == DeploymentKind.SAM_LAMBDA
    assert plan.access_mode == "aws_iam"
    assert plan.request.port is None
    assert plan.template_digest
    assert {item.service for item in plan.resources} >= {"Lambda", "DynamoDB", "Bedrock", "CodeBuild"}
    assert any("S3 Vectors" in text for text in plan.prerequisites)


def test_sam_blueprint_rejects_public_url_and_unapproved_permissions(tmp_path: Path) -> None:
    (tmp_path / "build.sh").write_text("#!/bin/sh\n")
    template = tmp_path / "template.yaml"
    template.write_text(SAM_TEMPLATE.replace("AuthType: AWS_IAM", "AuthType: NONE"))
    with pytest.raises(ValueError, match="IAM-protected"):
        load_sam_template(template)
    template.write_text(SAM_TEMPLATE.replace("bedrock:InvokeModel", "iam:CreateUser"))
    with pytest.raises(ValueError, match="unsupported permissions"):
        load_sam_template(template)
    template.write_text(SAM_TEMPLATE.replace("AWS::DynamoDB::Table", "AWS::RDS::DBInstance"))
    with pytest.raises(ValueError, match="unsupported resource types"):
        load_sam_template(template)
    template.write_text(SAM_TEMPLATE.replace("Type: AWS::DynamoDB::Table", "Type: AWS::DynamoDB::Table\n    DeletionPolicy: Retain"))
    with pytest.raises(ValueError, match="unsupported attributes"):
        load_sam_template(template)
    template.write_text(SAM_TEMPLATE.replace("Action: [bedrock:InvokeModel]", "NotAction: [iam:CreateUser]"))
    with pytest.raises(ValueError, match="unsupported clauses"):
        load_sam_template(template)
    template.write_text("Transform: [")
    with pytest.raises(ValueError, match="YAML is invalid"):
        load_sam_template(template)


def test_sam_package_only_replaces_function_code_uri(tmp_path: Path) -> None:
    (tmp_path / "build.sh").write_text("#!/bin/sh\n")
    path = tmp_path / "template.yaml"
    path.write_text(SAM_TEMPLATE)
    source = load_sam_template(path)
    packaged = json.loads(packaged_sam_template(source, "bucket", "sam-artifacts/id.zip"))
    assert packaged["Resources"]["Api"]["Properties"]["CodeUri"] == "s3://bucket/sam-artifacts/id.zip"
    assert source["Resources"]["Api"]["Properties"]["CodeUri"] == "build/"
    assert packaged["Outputs"]["Url"]["Value"] == {"Fn::GetAtt": ["ApiUrl", "FunctionUrl"]}


def test_sam_deployer_uses_isolated_build_and_smoke_checks_lambda(monkeypatch, tmp_path: Path) -> None:
    plan = sam_plan(monkeypatch)
    calls = []

    class S3:
        def upload_file(self, *args, **kwargs):
            calls.append(("upload", args, kwargs))

        def delete_object(self, **kwargs):
            calls.append(("delete", kwargs))

        def head_object(self, **kwargs):
            calls.append(("head", kwargs))

    class CodeBuild:
        def start_build(self, **kwargs):
            calls.append(("build", kwargs))
            return {"build": {"id": "sam-build:123"}}

    class CloudFormation:
        def create_change_set(self, **kwargs):
            calls.append(("change_set", kwargs))
            return {"Id": "changeset-1"}

        def execute_change_set(self, **kwargs):
            calls.append(("execute", kwargs))

        def describe_stacks(self, **kwargs):
            return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": [
                {"OutputKey": "Url", "OutputValue": "https://example.lambda-url.us-east-1.on.aws/"},
            ]}]}

        def describe_stack_resources(self, **kwargs):
            return {"StackResources": [{"LogicalResourceId": "Api", "PhysicalResourceId": "function-name"}]}

    class Lambda:
        def invoke(self, **kwargs):
            calls.append(("invoke", kwargs))
            return {"Payload": SimpleNamespace(read=lambda: b'{"statusCode":400}')}

    class Store:
        def set_status(self, _id, status, **changes):
            calls.append(("status", status, changes))

    deployer = AppAwsDeployer(
        region="us-east-1", source_bucket="source-bucket", build_project="container-build",
        sam_build_project="sam-build", sam_role_arn="arn:aws:iam::123:role/sam",
        platform_stack="AgentsPlatform-staging", poll_interval=0,
        clients={"s3": S3(), "codebuild": CodeBuild(), "cloudformation": CloudFormation(), "lambda": Lambda()},
    )

    def archive(_plan, workspace):
        source = workspace / "source"
        source.mkdir()
        (source / "template.yaml").write_text(SAM_TEMPLATE)
        (source / "build.sh").write_text("#!/bin/sh\n")
        zip_path = workspace / "source.zip"
        zip_path.write_bytes(b"zip")
        return zip_path

    monkeypatch.setattr(deployer, "_source_archive", archive)
    monkeypatch.setattr(deployer, "_wait_build", lambda _id: None)
    monkeypatch.setattr(deployer, "_wait_change_set", lambda *_: {"Changes": [
        {"ResourceChange": {"ResourceType": kind}} for kind in
        ("AWS::Lambda::Function", "AWS::IAM::Role", "AWS::DynamoDB::Table")
    ]})
    deployer.deploy(plan, Store())

    assert next(item for item in calls if item[0] == "build")[1]["projectName"] == "sam-build"
    assert next(item for item in calls if item[0] == "change_set")[1]["Capabilities"] == [
        "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND",
    ]
    assert next(item for item in calls if item[0] == "invoke")[1]["Payload"] == b'{"body":"{}"}'
    assert calls[-1][1] == DeploymentStatus.RUNNING


def test_sam_deployer_rejects_unexpected_change_set_and_deletes_artifact(monkeypatch, tmp_path: Path) -> None:
    plan = sam_plan(monkeypatch)
    calls = []

    class S3:
        def upload_file(self, *args, **kwargs):
            pass

        def delete_object(self, **kwargs):
            calls.append(("delete_object", kwargs))

        def head_object(self, **kwargs):
            pass

    class CodeBuild:
        def start_build(self, **kwargs):
            return {"build": {"id": "sam-build:123"}}

    class CloudFormation:
        def create_change_set(self, **kwargs):
            return {"Id": "changeset-1"}

        def execute_change_set(self, **kwargs):
            calls.append(("execute_change_set", kwargs))

        def describe_stacks(self, **kwargs):
            return {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}

        def delete_stack(self, **kwargs):
            calls.append(("delete_stack", kwargs))

        def get_waiter(self, _name):
            return SimpleNamespace(wait=lambda **kwargs: calls.append(("wait_delete", kwargs)))

    class Store:
        def set_status(self, _id, status, **changes):
            calls.append(("status", status))

    deployer = AppAwsDeployer(
        region="us-east-1", source_bucket="source-bucket", build_project="container-build",
        sam_build_project="sam-build", sam_role_arn="arn:aws:iam::123:role/sam",
        platform_stack="AgentsPlatform-staging", poll_interval=0,
        clients={"s3": S3(), "codebuild": CodeBuild(), "cloudformation": CloudFormation()},
    )

    def archive(_plan, workspace):
        source = workspace / "source"
        source.mkdir()
        (source / "template.yaml").write_text(SAM_TEMPLATE)
        (source / "build.sh").write_text("#!/bin/sh\n")
        zip_path = workspace / "source.zip"
        zip_path.write_bytes(b"zip")
        return zip_path

    monkeypatch.setattr(deployer, "_source_archive", archive)
    monkeypatch.setattr(deployer, "_wait_build", lambda _id: None)
    monkeypatch.setattr(deployer, "_wait_change_set", lambda *_: {"Changes": [
        {"ResourceChange": {"ResourceType": "AWS::RDS::DBInstance"}},
    ]})
    with pytest.raises(RuntimeError, match="unsupported resource types"):
        deployer.deploy(plan, Store())
    assert not any(name == "execute_change_set" for name, _ in calls)

    deployer.delete(plan, Store())
    assert any(name == "delete_stack" for name, _ in calls)
    assert any(name == "delete_object" and item["Key"] == deployer._sam_artifact_key(plan)
               for name, item in calls)
    assert not any(name == "delete_repository" for name, _ in calls)
