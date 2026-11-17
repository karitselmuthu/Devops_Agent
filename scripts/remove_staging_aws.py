"""Remove the Agents staging stack and its dedicated CDKToolkit bootstrap stack.

Without --execute, this command only prints a plan and makes no AWS calls.
"""

import argparse
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError

from scripts import teardown_aws


APP_STACK = "AgentsPlatform-staging"
BOOTSTRAP_STACK = "CDKToolkit"


def active_stacks(cloudformation: object) -> set[str]:
    return {
        stack["StackName"]
        for page in cloudformation.get_paginator("list_stacks").paginate()
        for stack in page["StackSummaries"]
        if stack["StackStatus"] != "DELETE_COMPLETE"
    }


def require_exclusive_bootstrap(cloudformation: object) -> set[str]:
    names = active_stacks(cloudformation)
    other = names - {APP_STACK, BOOTSTRAP_STACK}
    if other:
        raise ValueError("CDKToolkit may be shared; other active stacks exist: " + ", ".join(sorted(other)))
    if BOOTSTRAP_STACK not in names:
        raise ValueError("CDKToolkit stack was not found in this account and Region")
    return names


def bootstrap_assets(cloudformation: object, account_id: str, region: str) -> tuple[dict, str, str]:
    stack = cloudformation.describe_stacks(StackName=BOOTSTRAP_STACK)["Stacks"][0]
    resources = [
        item for page in cloudformation.get_paginator("list_stack_resources").paginate(
            StackName=stack["StackId"]
        ) for item in page["StackResourceSummaries"]
    ]
    buckets = [item["PhysicalResourceId"] for item in resources if item["ResourceType"] == "AWS::S3::Bucket"]
    repositories = [item["PhysicalResourceId"] for item in resources
                    if item["ResourceType"] == "AWS::ECR::Repository"]
    if len(buckets) != 1 or len(repositories) != 1:
        raise ValueError("Expected one CDK bootstrap S3 bucket and one ECR repository")
    suffix = f"-{account_id}-{region}"
    if not all(name.startswith("cdk-") and name.endswith(suffix)
               for name in (*buckets, *repositories)):
        raise ValueError("Bootstrap asset names do not match the expected account and Region")
    return stack, buckets[0], repositories[0]


def empty_bucket(s3: object, bucket: str) -> None:
    while True:
        page = s3.list_object_versions(Bucket=bucket, MaxKeys=1000)
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in (*page.get("Versions", []), *page.get("DeleteMarkers", []))
        ]
        if not objects:
            break
        result = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if result.get("Errors"):
            raise RuntimeError(f"Could not delete all versions from {bucket}: {result['Errors']}")
    while True:
        page = s3.list_objects_v2(Bucket=bucket, MaxKeys=1000)
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if not objects:
            break
        result = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        if result.get("Errors"):
            raise RuntimeError(f"Could not empty {bucket}: {result['Errors']}")


def empty_repository(ecr: object, repository: str) -> None:
    while True:
        images = ecr.list_images(repositoryName=repository, maxResults=100).get("imageIds", [])
        if not images:
            break
        result = ecr.batch_delete_image(repositoryName=repository, imageIds=images)
        if result.get("failures"):
            raise RuntimeError(f"Could not empty ECR repository {repository}: {result['failures']}")


def delete_bootstrap(session: boto3.Session, account_id: str, region: str) -> None:
    cloudformation = session.client("cloudformation")
    require_exclusive_bootstrap(cloudformation)
    stack, bucket, repository = bootstrap_assets(cloudformation, account_id, region)
    s3 = session.client("s3")
    ecr = session.client("ecr")
    empty_bucket(s3, bucket)
    empty_repository(ecr, repository)
    if stack.get("EnableTerminationProtection"):
        cloudformation.update_termination_protection(
            StackName=BOOTSTRAP_STACK, EnableTerminationProtection=False,
        )
    cloudformation.delete_stack(StackName=BOOTSTRAP_STACK)
    cloudformation.get_waiter("stack_delete_complete").wait(StackName=stack["StackId"])

    retained = [
        item for page in cloudformation.get_paginator("list_stack_resources").paginate(
            StackName=stack["StackId"]
        ) for item in page["StackResourceSummaries"]
        if item["ResourceStatus"] == "DELETE_SKIPPED"
    ]
    unexpected = [item for item in retained if item["ResourceType"] not in {
        "AWS::S3::Bucket", "AWS::ECR::Repository",
    }]
    if unexpected:
        names = ", ".join(item["PhysicalResourceId"] for item in unexpected)
        raise RuntimeError(f"CDKToolkit deleted but retained resources need manual review: {names}")
    for item in retained:
        if item["ResourceType"] == "AWS::S3::Bucket":
            s3.delete_bucket(Bucket=item["PhysicalResourceId"])
        elif item["ResourceType"] == "AWS::ECR::Repository":
            ecr.delete_repository(repositoryName=item["PhysicalResourceId"])
    print("Deleted CDKToolkit and its bootstrap assets.")


def execute(region: str, account_id: str) -> None:
    if len(account_id) != 12 or not account_id.isdigit():
        raise ValueError("--account-id must be a 12-digit AWS account ID")
    session = teardown_aws.aws_session(region)
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != account_id:
        raise ValueError(f"AWS account mismatch: expected {account_id}, got {actual_account}")
    cloudformation = session.client("cloudformation")
    names = require_exclusive_bootstrap(cloudformation)
    _, bucket, repository = bootstrap_assets(cloudformation, account_id, region)
    print(f"Staging stack: {APP_STACK if APP_STACK in names else 'already absent'}")
    print(f"CDK bootstrap: {BOOTSTRAP_STACK}; S3: {bucket}; ECR: {repository}")
    phrase = f"DELETE STAGING AND CDK {account_id} {region}"
    if input(f"Type '{phrase}' to proceed: ").strip() != phrase:
        raise ValueError("Cleanup cancelled: confirmation did not match")
    if APP_STACK in names:
        teardown_aws.execute("staging", region, account_id)
    require_exclusive_bootstrap(cloudformation)
    delete_bootstrap(session, account_id, region)
    print("Staging application and CDK bootstrap cleanup completed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(f"Plan only: delete {APP_STACK}, then {BOOTSTRAP_STACK} in {args.account_id}/{args.region}.")
        print("Execution refuses to delete shared bootstrap assets while other active CloudFormation stacks exist.")
        print("It requires typed confirmations and removes the bootstrap S3 objects and ECR images.")
        print("Add --execute to run. No AWS calls were made.")
        return 0
    try:
        execute(args.region, args.account_id)
        return 0
    except (ClientError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
