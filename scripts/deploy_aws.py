"""Build, migrate, deploy, and smoke-test an AWS environment from CI."""

import os
import subprocess
import time

import boto3
import httpx
from botocore.exceptions import ClientError


def run(*args: str) -> None:
    subprocess.run(args, check=True)


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


def main() -> int:
    environment = require_environment("DEPLOY_ENVIRONMENT")
    if environment not in {"staging", "production"}:
        raise ValueError("Unsupported environment")
    api_count, background_count = deployment_counts(environment)
    require_environment("AWS_REGION")
    github_token = require_environment("AGENTS_GITHUB_TOKEN")
    openai_key = require_environment("AGENTS_OPENAI_API_KEY")
    domain = require_environment("API_DOMAIN")
    domain_parameters = {
        "CertificateArn": require_environment("CERTIFICATE_ARN"),
        "DomainName": domain,
        "HostedZoneId": require_environment("HOSTED_ZONE_ID"),
    }
    stack_name = f"AgentsPlatform-{environment}"
    cloudformation = boto3.client("cloudformation")
    ecs = boto3.client("ecs")
    stack = describe_stack(cloudformation, stack_name)
    if stack is None:
        deploy_stack(
            stack_name, environment,
            {
                **domain_parameters,
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
    secrets = boto3.client("secretsmanager")
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
    run("docker", "push", f"{repository_uri}:{image_tag}")

    deploy_stack(stack_name, environment, {
        **domain_parameters,
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
                "ApiDesiredCount": str(api_count),
                "BackgroundDesiredCount": str(background_count),
                "AllowedRepositories": os.environ.get("ALLOWED_REPOSITORIES", ""),
                "AllowedPipelines": os.environ.get("ALLOWED_PIPELINES", ""),
                "AllowedStacks": os.environ.get("ALLOWED_STACKS", ""),
                "OpenAIModel": require_environment("OPENAI_MODEL"),
            },
        )
        url = f"https://{domain}/health"
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
                "ApiDesiredCount": previous_api_count,
                "BackgroundDesiredCount": previous_background_count,
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
