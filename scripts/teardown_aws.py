"""Prepare a CDK stack for full deletion and destroy it only on explicit request."""

import argparse
import os
import subprocess
import sys
import json

import boto3
from botocore.exceptions import ClientError, MissingDependencyException


def cdk_command(action: str, stack_name: str, environment: str) -> list[str]:
    return [
        "npx", "--yes", "aws-cdk@2.1148.1", action, stack_name,
        "-c", f"environment={environment}", "-c", "teardown=true",
    ]


def aws_session(region: str) -> boto3.Session:
    """Use boto3 credentials, with an AWS CLI login fallback for older botocore."""
    session = boto3.Session(region_name=region)
    try:
        if session.get_credentials() is not None:
            return session
    except MissingDependencyException:
        pass
    exported = subprocess.run(
        ["aws", "configure", "export-credentials", "--format", "process"],
        check=True, capture_output=True, text=True,
    )
    credentials = json.loads(exported.stdout)
    return boto3.Session(
        region_name=region,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials.get("SessionToken"),
    )


def parameter_arguments(stack: dict) -> list[str]:
    values = {item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]}
    required = {"CertificateArn", "DomainName", "HostedZoneId"}
    if not required <= values.keys():
        raise ValueError("This stack predates the HTTPS template; migrate it before teardown")
    supported = required | {
        "ImageTag", "MigrationImageTag", "AllowedRepositories",
        "AllowedPipelines", "AllowedStacks", "OpenAIModel",
    }
    values = {key: value for key, value in values.items() if key in supported}
    values.update(ApiDesiredCount="0", BackgroundDesiredCount="0")
    return [argument for key, value in sorted(values.items())
            for argument in ("--parameters", f"{key}={value}")]


def execute(environment: str, region: str, account_id: str) -> None:
    if len(account_id) != 12 or not account_id.isdigit():
        raise ValueError("--account-id must be a 12-digit AWS account ID")
    stack_name = f"AgentsPlatform-{environment}"
    session = aws_session(region)
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != account_id:
        raise ValueError(f"AWS account mismatch: expected {account_id}, got {actual_account}")
    cloudformation = session.client("cloudformation")
    stack = cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]
    if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
        raise ValueError(f"Stack is not ready for teardown: {stack['StackStatus']}")
    parameters = parameter_arguments(stack)
    command_environment = os.environ.copy()
    command_environment.update(
        AWS_REGION=region, AWS_DEFAULT_REGION=region,
        CDK_DEFAULT_ACCOUNT=account_id, CDK_DEFAULT_REGION=region,
    )
    subprocess.run(cdk_command("diff", stack_name, environment) + ["--method", "template"],
                   check=True, env=command_environment)
    phrase = f"DELETE {stack_name} {account_id} {region}"
    if input(f"Type '{phrase}' to delete this stack and its data: ").strip() != phrase:
        raise ValueError("Teardown cancelled: confirmation did not match")

    subprocess.run(
        cdk_command("deploy", stack_name, environment)
        + ["--require-approval", "never", *parameters],
        check=True, env=command_environment,
    )
    if environment == "production":
        cloudformation.update_termination_protection(
            StackName=stack_name, EnableTerminationProtection=False,
        )
    subprocess.run(cdk_command("destroy", stack_name, environment) + ["--force"],
                   check=True, env=command_environment)

    remaining = [
        resource for page in cloudformation.get_paginator("list_stack_resources").paginate(
            StackName=stack["StackId"]
        ) for resource in page["StackResourceSummaries"]
        if resource["ResourceStatus"] == "DELETE_SKIPPED"
    ]
    if remaining:
        names = ", ".join(item["PhysicalResourceId"] for item in remaining)
        raise RuntimeError(f"Stack deleted with retained resources: {names}")
    print(f"Deleted {stack_name}. Check separately for manual RDS snapshots and shared CDK bootstrap resources.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("staging", "production"))
    parser.add_argument("--region", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--execute", action="store_true", help="Run the destructive teardown")
    args = parser.parse_args()
    stack_name = f"AgentsPlatform-{args.environment}"
    if not args.execute:
        print(f"Plan only: {stack_name} in {args.account_id}/{args.region}.")
        print("Execution will show a CDK diff and require exact typed confirmation.")
        print("It will stop all eight ECS services, enable destructive removal policies,")
        print("disable production stack termination protection, and destroy the stack.")
        print("Add --execute to perform these actions. No AWS calls were made.")
        return 0
    try:
        execute(args.environment, args.region, args.account_id)
        return 0
    except (ClientError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Teardown failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
