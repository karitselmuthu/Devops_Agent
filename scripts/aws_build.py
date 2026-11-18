"""Set up AWS deployment access or build and deploy the agents application."""

import argparse
import base64
import json
import os
import subprocess
import sys
import time

import boto3
import httpx
from botocore.exceptions import ClientError, MissingDependencyException


REPOSITORY = "karitselmuthu/Devops_Agent"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def output_text(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def deployment_role_name(environment: str) -> str:
    return f"DevopsAgentGitHub{environment.title()}"


def aws_session(region: str) -> boto3.Session:
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


def login_ecr(session: boto3.Session) -> None:
    authorization = session.client("ecr").get_authorization_token()["authorizationData"][0]
    username, password = base64.b64decode(authorization["authorizationToken"]).decode().split(":", 1)
    subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", authorization["proxyEndpoint"]],
        input=password, text=True, check=True,
    )


def setup_policy(account_id: str, region: str, environment: str) -> dict:
    stack = f"AgentsPlatform-{environment}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "AssumeCdkBootstrapRoles", "Effect": "Allow", "Action": "sts:AssumeRole",
             "Resource": f"arn:aws:iam::{account_id}:role/cdk-hnb659fds-*-{account_id}-{region}"},
            {"Sid": "ReadStack", "Effect": "Allow", "Action": "cloudformation:DescribeStacks",
             "Resource": f"arn:aws:cloudformation:{region}:{account_id}:stack/{stack}/*"},
            {"Sid": "WriteAppSecrets", "Effect": "Allow", "Action": "secretsmanager:PutSecretValue",
             "Resource": [f"arn:aws:secretsmanager:{region}:{account_id}:secret:GitHubToken*",
                          f"arn:aws:secretsmanager:{region}:{account_id}:secret:OpenAIKey*"]},
            {"Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
            {"Sid": "EcrPush", "Effect": "Allow",
             "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
                        "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
                        "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
             "Resource": f"arn:aws:ecr:{region}:{account_id}:repository/agentsplatform-{environment}-*"},
            {"Sid": "RunMigrationTask", "Effect": "Allow", "Action": "ecs:RunTask",
             "Resource": f"arn:aws:ecs:{region}:{account_id}:task-definition/AgentsPlatform*:*"},
            {"Sid": "WatchMigrationTask", "Effect": "Allow", "Action": "ecs:DescribeTasks",
             "Resource": f"arn:aws:ecs:{region}:{account_id}:task/{stack}-*/*"},
            {"Sid": "PassTaskRoles", "Effect": "Allow", "Action": "iam:PassRole",
             "Resource": f"arn:aws:iam::{account_id}:role/{stack}-*",
             "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}},
        ],
    }


def setup_access(region: str, environment: str) -> None:
    account_id = output_text("aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text")
    repo = json.loads(output_text("gh", "api", f"repos/{REPOSITORY}"))
    stacks = json.loads(output_text("aws", "cloudformation", "list-stacks", "--region", region))["StackSummaries"]
    bootstrap_exists = any(stack["StackName"] == "CDKToolkit" and stack["StackStatus"] != "DELETE_COMPLETE"
                           for stack in stacks)
    if not bootstrap_exists:
        buckets = json.loads(output_text("aws", "s3api", "list-buckets"))["Buckets"]
        expected_bucket = f"cdk-hnb659fds-assets-{account_id}-{region}"
        if expected_bucket in {bucket["Name"] for bucket in buckets}:
            raise ValueError("Old CDK asset bucket remains; clean it with aws_cleanup.py before bootstrapping")
    subject = (f"repo:{repo['owner']['login']}@{repo['owner']['id']}/"
               f"{repo['name']}@{repo['id']}:environment:{environment}")
    print(f"AWS account: {account_id}; Region: {region}; GitHub subject: {subject}")
    if input("Create or update the AWS role, CDK bootstrap, and GitHub environment? [y/N] ").strip() != "y":
        print("Setup cancelled.")
        return
    provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/token.actions.githubusercontent.com"
    role_name = deployment_role_name(environment)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    providers = json.loads(output_text("aws", "iam", "list-open-id-connect-providers"))
    if provider_arn not in {item["Arn"] for item in providers["OpenIDConnectProviderList"]}:
        run("aws", "iam", "create-open-id-connect-provider", "--url",
            "https://token.actions.githubusercontent.com", "--client-id-list", "sts.amazonaws.com")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Principal": {"Federated": provider_arn},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": subject,
                "token.actions.githubusercontent.com:ref": "refs/heads/main",
            }},
        }],
    }
    roles = json.loads(output_text("aws", "iam", "list-roles"))["Roles"]
    if role_name in {role["RoleName"] for role in roles}:
        run("aws", "iam", "update-assume-role-policy", "--role-name", role_name,
            "--policy-document", json.dumps(trust))
    else:
        run("aws", "iam", "create-role", "--role-name", role_name,
            "--description", f"GitHub Actions deploy role for {REPOSITORY} ({environment})",
            "--assume-role-policy-document", json.dumps(trust))
    run("aws", "iam", "put-role-policy", "--role-name", role_name,
        "--policy-name", "deploy", "--policy-document",
        json.dumps(setup_policy(account_id, region, environment)))
    if not bootstrap_exists:
        run("npx", "--yes", "aws-cdk@2.1148.1", "bootstrap", f"aws://{account_id}/{region}")
    branch_policy = {"deployment_branch_policy": {
        "protected_branches": False, "custom_branch_policies": True,
    }}
    subprocess.run(
        ["gh", "api", "-X", "PUT", f"repos/{REPOSITORY}/environments/{environment}", "--input", "-"],
        input=json.dumps(branch_policy), text=True, check=True,
    )
    existing_policies = json.loads(output_text(
        "gh", "api", f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies"
    ))
    if not any(item["name"] == "main" for item in existing_policies.get("branch_policies", [])):
        run("gh", "api", "-X", "POST",
            f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies",
            "-f", "name=main", "-f", "type=branch")
    run("gh", "variable", "set", "AWS_DEPLOY_ROLE_ARN", "--env", environment,
        "--repo", REPOSITORY, "--body", role_arn)
    run("gh", "variable", "set", "AWS_REGION", "--env", environment,
        "--repo", REPOSITORY, "--body", region)
    print(f"AWS OIDC setup complete: {role_arn}")


def deploy_stack(stack_name: str, environment: str, parameters: dict[str, str]) -> None:
    command = [
        "npx", "--yes", "aws-cdk@2.1148.1", "deploy", stack_name,
        "-c", f"environment={environment}", "--require-approval", "never",
    ]
    for key, value in parameters.items():
        command.extend(["--parameters", f"{key}={value}"])
    run(*command)


def describe_stack(client: object, stack_name: str) -> dict | None:
    try:
        return client.describe_stacks(StackName=stack_name)["Stacks"][0]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ValidationError":
            return None
        raise


def output(stack: dict, key: str) -> str:
    return next(item["OutputValue"] for item in stack["Outputs"] if item["OutputKey"] == key)


def parameter(stack: dict, key: str) -> str:
    return next(item["ParameterValue"] for item in stack["Parameters"] if item["ParameterKey"] == key)


def previous_counts(stack: dict) -> tuple[str, str]:
    values = {item["ParameterKey"]: item["ParameterValue"] for item in stack["Parameters"]}
    legacy_count = values.get("DesiredCount", "0")
    return values.get("ApiDesiredCount", legacy_count), values.get("BackgroundDesiredCount", legacy_count)


def require_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Missing deployment setting: {name}")
    return value


def deployment_counts(environment: str) -> tuple[int, int]:
    background = os.environ.get("BACKGROUND_DESIRED_COUNT", "1")
    if background not in {"0", "1"}:
        raise ValueError("BACKGROUND_DESIRED_COUNT must be 0 or 1")
    if environment == "production" and background != "1":
        raise ValueError("Production requires background workers")
    return (2 if environment == "production" else 1), int(background)


def endpoint_parameters(environment: str) -> dict[str, str]:
    if environment == "production":
        return {
            "CertificateArn": require_environment("CERTIFICATE_ARN"),
            "DomainName": require_environment("API_DOMAIN"),
            "HostedZoneId": require_environment("HOSTED_ZONE_ID"),
        }
    return {"CloudFrontPrefixListId": require_environment("CLOUDFRONT_PREFIX_LIST_ID")}


def deploy() -> int:
    environment = require_environment("DEPLOY_ENVIRONMENT")
    if environment not in {"staging", "production"}:
        raise ValueError("Unsupported environment")
    api_count, background_count = deployment_counts(environment)
    region = require_environment("AWS_REGION")
    github_token = require_environment("AGENTS_GITHUB_TOKEN")
    openai_key = require_environment("AGENTS_OPENAI_API_KEY")
    endpoint_settings = endpoint_parameters(environment)
    stack_name = f"AgentsPlatform-{environment}"
    session = aws_session(region)
    cloudformation = session.client("cloudformation")
    ecs = session.client("ecs")
    stack = describe_stack(cloudformation, stack_name)
    if stack is None:
        deploy_stack(
            stack_name, environment,
            {
                **endpoint_settings,
                "AllowedRepositories": os.environ.get("ALLOWED_REPOSITORIES", ""),
                "AllowedPipelines": os.environ.get("ALLOWED_PIPELINES", ""),
                "AllowedStacks": os.environ.get("ALLOWED_STACKS", ""),
                "OpenAIModel": require_environment("OPENAI_MODEL"),
                "ApiDesiredCount": "0",
                "BackgroundDesiredCount": "0",
            },
        )
        stack = describe_stack(cloudformation, stack_name)
    previous_image_tag = parameter(stack, "ImageTag")
    previous_api_count, previous_background_count = previous_counts(stack)
    client_parameters = (
        {"StagingCognitoClientId": output(stack, "UserPoolClientId")}
        if environment == "staging" else {}
    )
    secrets = session.client("secretsmanager")
    for output_name, secret_value in (
        ("GitHubTokenSecretArn", github_token),
        ("OpenAIKeySecretArn", openai_key),
    ):
        secrets.put_secret_value(
            SecretId=output(stack, output_name),
            SecretString=secret_value,
        )
    repository_uri = output(stack, "RepositoryUri")
    image_tag = require_environment("GITHUB_SHA")
    run("docker", "build", "--platform", "linux/amd64", "-t", f"{repository_uri}:{image_tag}", ".")
    login_ecr(session)
    run("docker", "push", f"{repository_uri}:{image_tag}")

    deploy_stack(stack_name, environment, {
        **endpoint_settings,
        **client_parameters,
        "MigrationImageTag": image_tag,
        "ApiDesiredCount": previous_api_count,
        "BackgroundDesiredCount": previous_background_count,
    })
    stack = describe_stack(cloudformation, stack_name)
    response = ecs.run_task(
        cluster=output(stack, "ClusterArn"),
        taskDefinition=output(stack, "MigrationTaskArn"),
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": output(stack, "PrivateSubnetIds").split(","),
                "securityGroups": [output(stack, "WorkerSecurityGroupId")],
                "assignPublicIp": "DISABLED",
            }
        },
    )
    if response.get("failures") or not response.get("tasks"):
        raise RuntimeError("Migration task could not start")
    task_arn = response["tasks"][0]["taskArn"]
    cluster_arn = output(stack, "ClusterArn")
    ecs.get_waiter("tasks_stopped").wait(cluster=cluster_arn, tasks=[task_arn])
    task = ecs.describe_tasks(cluster=cluster_arn, tasks=[task_arn])["tasks"][0]
    if task["containers"][0].get("exitCode") != 0:
        raise RuntimeError("Database migration failed")

    try:
        deploy_stack(
            stack_name, environment,
            {
                "ImageTag": image_tag,
                **client_parameters,
                "ApiDesiredCount": str(api_count),
                "BackgroundDesiredCount": str(background_count),
                "AllowedRepositories": os.environ.get("ALLOWED_REPOSITORIES", ""),
                "AllowedPipelines": os.environ.get("ALLOWED_PIPELINES", ""),
                "AllowedStacks": os.environ.get("ALLOWED_STACKS", ""),
                "OpenAIModel": require_environment("OPENAI_MODEL"),
            },
        )
        url = f"https://{output(stack, 'ApiDomain')}/health"
        print(f"Health check: {url}")
        for attempt in range(12):
            try:
                if httpx.get(url, timeout=10).status_code == 200:
                    return 0
            except httpx.HTTPError:
                pass
            time.sleep(10)
        raise RuntimeError("Deployment health check failed")
    except Exception:
        deploy_stack(
            stack_name, environment,
            {
                "ImageTag": previous_image_tag,
                **client_parameters,
                "ApiDesiredCount": previous_api_count,
                "BackgroundDesiredCount": previous_background_count,
            },
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Create GitHub OIDC access and bootstrap CDK")
    setup.add_argument("--region", required=True)
    setup.add_argument("--environment", choices=("staging", "production"), default="staging")
    setup.add_argument("--execute", action="store_true")
    commands.add_parser("deploy", help="Build, migrate, deploy, and check the application")
    args = parser.parse_args()
    try:
        if args.command == "deploy":
            return deploy()
        if not args.execute:
            print(f"Plan only: set up {args.environment} OIDC and CDK bootstrap in {args.region}.")
            print("Add --execute to create or update AWS and GitHub resources. No external calls were made.")
            return 0
        setup_access(args.region, args.environment)
        return 0
    except (ClientError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"AWS build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
