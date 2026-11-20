"""Restricted SAM blueprint for repository supplied Python Lambda applications."""

import hashlib
import json
from pathlib import Path

import yaml

SAM_TRANSFORM = "AWS::Serverless-2016-10-31"
ALLOWED_ACTIONS = {
    "bedrock:InvokeModel", "s3:ListAllMyBuckets", "lambda:ListFunctions",
    "s3vectors:QueryVectors", "s3vectors:GetVectors",
}


class _CloudFormationLoader(yaml.SafeLoader):
    pass


def _intrinsic(loader: _CloudFormationLoader, suffix: str, node: yaml.Node) -> dict:
    if suffix not in {"Ref", "Sub", "GetAtt"}:
        raise ValueError(f"Unsupported CloudFormation intrinsic: !{suffix}")
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        raise ValueError(f"Unsupported !{suffix} value")
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {suffix if suffix == "Ref" else f"Fn::{suffix}": value}


_CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def load_sam_template(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValueError("A root SAM template.yaml is required")
    raw = path.read_bytes()
    if len(raw) > 51200:
        raise ValueError("SAM template exceeds the inline CloudFormation limit")
    try:
        template = yaml.load(raw, Loader=_CloudFormationLoader)
    except yaml.YAMLError as exc:
        raise ValueError("SAM template YAML is invalid") from exc
    if not isinstance(template, dict) or template.get("Transform") != SAM_TRANSFORM:
        raise ValueError("Only the AWS SAM transform is supported")
    if set(template) - {"AWSTemplateFormatVersion", "Transform", "Description", "Parameters", "Resources", "Outputs"}:
        raise ValueError("SAM template contains unsupported top-level sections")
    seen: set[int] = set()
    active: set[int] = set()

    def check_intrinsics(value) -> None:
        if isinstance(value, dict):
            if id(value) in active:
                raise ValueError("SAM template contains a cyclic YAML alias")
            if id(value) in seen:
                return
            seen.add(id(value))
            active.add(id(value))
            for key, child in value.items():
                if isinstance(key, str) and key.startswith("Fn::") and key not in {"Fn::Sub", "Fn::GetAtt"}:
                    raise ValueError("SAM template contains an unsupported intrinsic")
                check_intrinsics(child)
            active.remove(id(value))
        elif isinstance(value, list):
            if id(value) in active:
                raise ValueError("SAM template contains a cyclic YAML alias")
            if id(value) in seen:
                return
            seen.add(id(value))
            active.add(id(value))
            for child in value:
                check_intrinsics(child)
            active.remove(id(value))

    check_intrinsics(template)
    resources = template.get("Resources")
    if not isinstance(resources, dict) or not resources:
        raise ValueError("SAM template has no resources")
    if any(not isinstance(item, dict) for item in resources.values()):
        raise ValueError("SAM resources must be objects")
    if any(set(item) != {"Type", "Properties"} for item in resources.values()):
        raise ValueError("SAM resources contain unsupported attributes")
    kinds = {item.get("Type") for item in resources.values()}
    if kinds - {"AWS::Serverless::Function", "AWS::DynamoDB::Table"}:
        raise ValueError("SAM template contains unsupported resource types")
    functions = [(name, item) for name, item in resources.items() if item.get("Type") == "AWS::Serverless::Function"]
    if len(functions) != 1 or functions[0][0] != "Api":
        raise ValueError("This SAM blueprint requires one Api Lambda function")
    name, function = functions[0]
    props = function.get("Properties", {})
    if not isinstance(props, dict):
        raise ValueError("SAM function properties must be an object")
    if set(props) - {"CodeUri", "Handler", "Runtime", "Timeout", "MemorySize",
                      "Environment", "FunctionUrlConfig", "Policies"}:
        raise ValueError("SAM function contains unsupported properties")
    if props.get("Runtime") != "python3.12" or props.get("CodeUri") != "build/":
        raise ValueError("This SAM blueprint requires Python 3.12 and CodeUri build/")
    if props.get("FunctionUrlConfig") != {"AuthType": "AWS_IAM"}:
        raise ValueError("Only IAM-protected Lambda Function URLs are supported")
    if int(props.get("MemorySize", 128)) > 1024 or int(props.get("Timeout", 3)) > 180:
        raise ValueError("Lambda memory or timeout exceeds this blueprint's limits")
    for policy in props.get("Policies", []):
        if not isinstance(policy, dict):
            raise ValueError("Unsupported SAM policy")
        if "DynamoDBCrudPolicy" in policy:
            if set(policy) != {"DynamoDBCrudPolicy"}:
                raise ValueError("Unsupported DynamoDB policy")
            if not isinstance(policy["DynamoDBCrudPolicy"], dict):
                raise ValueError("Unsupported DynamoDB policy")
            if set(policy["DynamoDBCrudPolicy"]) != {"TableName"}:
                raise ValueError("Unsupported DynamoDB policy")
            table = policy["DynamoDBCrudPolicy"].get("TableName")
            if table not in ({"Ref": item} for item, resource in resources.items()
                             if resource.get("Type") == "AWS::DynamoDB::Table"):
                raise ValueError("DynamoDB policy must reference a table in this template")
        elif "Statement" in policy:
            if set(policy) != {"Statement"}:
                raise ValueError("Unsupported SAM policy statement")
            statements = policy["Statement"]
            if not isinstance(statements, list):
                raise ValueError("Unsupported SAM policy statement")
            for statement in statements:
                if not isinstance(statement, dict):
                    raise ValueError("Unsupported SAM policy statement")
                if set(statement) - {"Effect", "Action", "Resource"}:
                    raise ValueError("SAM policy contains unsupported clauses")
                actions = statement.get("Action", [])
                actions = [actions] if isinstance(actions, str) else actions
                if not actions or statement.get("Effect") != "Allow" or set(actions) - ALLOWED_ACTIONS:
                    raise ValueError("SAM policy contains unsupported permissions")
                policy_resources = statement.get("Resource", [])
                policy_resources = [policy_resources] if isinstance(policy_resources, (str, dict)) else policy_resources
                if not isinstance(policy_resources, list) or not policy_resources:
                    raise ValueError("SAM policy must list resources")
                for resource in policy_resources:
                    if resource == "*":
                        if any(action not in {"s3:ListAllMyBuckets", "lambda:ListFunctions"}
                               for action in actions):
                            raise ValueError("SAM model and vector permissions must be resource-scoped")
                    elif not (isinstance(resource, str) and resource.startswith("arn:")) and not (
                        isinstance(resource, dict) and isinstance(resource.get("Fn::Sub"), str)
                        and resource["Fn::Sub"].startswith("arn:")
                    ):
                        raise ValueError("SAM policy resource must be an ARN")
        else:
            raise ValueError("SAM policy template is unsupported")
    for resource in resources.values():
        if resource.get("Type") == "AWS::DynamoDB::Table":
            table_props = resource.get("Properties", {})
            if set(table_props) - {"BillingMode", "AttributeDefinitions", "KeySchema", "TimeToLiveSpecification"}:
                raise ValueError("DynamoDB table contains unsupported properties")
            if table_props.get("BillingMode") != "PAY_PER_REQUEST":
                raise ValueError("Only on-demand DynamoDB tables are supported")
    if template.get("Outputs") != {"Url": {"Value": {"Fn::GetAtt": ["ApiUrl", "FunctionUrl"]}}}:
        raise ValueError("SAM template must output the IAM-protected Function URL")
    if not (path.parent / "build.sh").is_file() or (path.parent / "build.sh").is_symlink():
        raise ValueError("This SAM blueprint requires a root build.sh")
    return template


def sam_template_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def packaged_sam_template(template: dict, artifact_bucket: str, artifact_key: str) -> str:
    packaged = json.loads(json.dumps(template))
    for resource in packaged["Resources"].values():
        if resource["Type"] == "AWS::Serverless::Function":
            resource["Properties"]["CodeUri"] = f"s3://{artifact_bucket}/{artifact_key}"
    return json.dumps(packaged)


def sam_resources() -> list:
    from agents_platform.app_deployments import PlannedResource

    return [
        PlannedResource(service="CodeBuild", name="Isolated SAM package build", purpose="Build Lambda zip", billed_while_running=True),
        PlannedResource(service="S3", name="Lambda deployment artifact", purpose="Hold the build package until cleanup"),
        PlannedResource(service="Lambda", name="Python 3.12 function", purpose="Run requests on demand"),
        PlannedResource(service="IAM", name="Lambda execution role", purpose="Grant reviewed DynamoDB, Bedrock, vector-read, and logging permissions"),
        PlannedResource(service="DynamoDB", name="On-demand memory table", purpose="Store session and user memory"),
        PlannedResource(service="Lambda Function URL", name="IAM-protected endpoint", purpose="Accept SigV4-signed requests"),
        PlannedResource(service="Bedrock", name="Model invocation", purpose="Use enabled models; request charges apply"),
    ]
