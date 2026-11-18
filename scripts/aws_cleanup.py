"""Inspect or delete the Agents application, CDK bootstrap, and staging leftovers."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError, MissingDependencyException

STACK_NAME = "AgentsPlatform-staging"
ROLE_NAME = "DevopsAgentGitHubStaging"
BOOTSTRAP_STACK = "CDKToolkit"
APP_STACK_PREFIX = "AgentsApp-"
DEFAULT_REPORT = Path("AgentsPlatform-staging-deletion-report.json")


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


def cdk_command(action: str, stack_name: str, environment: str) -> list[str]:
    return [
        "npx", "--yes", "aws-cdk@2.1148.1", action, stack_name,
        "-c", f"environment={environment}", "-c", "teardown=true",
    ]


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


def active_stacks(cloudformation: object) -> set[str]:
    return {
        stack["StackName"]
        for page in cloudformation.get_paginator("list_stacks").paginate()
        for stack in page["StackSummaries"]
        if stack["StackStatus"] != "DELETE_COMPLETE"
    }


def managed_app_stacks(cloudformation: object, environment: str) -> list[tuple[str, str]]:
    """Return only application stacks tagged by this platform."""
    prefix = f"{APP_STACK_PREFIX}{environment}-"
    found = []
    for name in sorted(active_stacks(cloudformation)):
        if not name.startswith(prefix):
            continue
        stack = cloudformation.describe_stacks(StackName=name)["Stacks"][0]
        tags = {tag["Key"]: tag["Value"] for tag in stack.get("Tags", [])}
        suffix = name.removeprefix(prefix)
        if tags.get("ManagedBy") != "AgentsPlatform" or len(suffix) != 12 or any(
            char not in "0123456789abcdef" for char in suffix
        ):
            raise ValueError(f"Application stack {name} lacks the expected ownership tags or name")
        found.append((name, f"agents-app-{environment}-{suffix}"))
    return found


def delete_managed_apps(cloudformation: object, ecr: object, environment: str) -> None:
    for stack_name, repository in managed_app_stacks(cloudformation, environment):
        print(f"Deleting application stack {stack_name} and ECR repository {repository}")
        cloudformation.delete_stack(StackName=stack_name)
        cloudformation.get_waiter("stack_delete_complete").wait(StackName=stack_name)
    prefix = f"agents-app-{environment}-"
    for page in ecr.get_paginator("describe_repositories").paginate():
        for item in page["repositories"]:
            repository = item["repositoryName"]
            if not repository.startswith(prefix):
                continue
            tags = {tag["Key"]: tag["Value"] for tag in
                    ecr.list_tags_for_resource(resourceArn=item["repositoryArn"]).get("tags", [])}
            suffix = repository.removeprefix(prefix)
            if tags.get("ManagedBy") != "AgentsPlatform" or len(suffix) != 12 or any(
                char not in "0123456789abcdef" for char in suffix
            ):
                raise ValueError(f"Application repository {repository} lacks the expected ownership tags or name")
            print(f"Deleting application ECR repository {repository}")
            try:
                ecr.delete_repository(repositoryName=repository, force=True)
            except ecr.exceptions.RepositoryNotFoundException:
                pass


def require_exclusive_bootstrap(cloudformation: object) -> set[str]:
    names = active_stacks(cloudformation)
    other = names - {STACK_NAME, BOOTSTRAP_STACK}
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


def app_resource_ids(cloudformation: object, stack_name: str) -> tuple[str | None, str | None]:
    resources = [
        item for page in cloudformation.get_paginator("list_stack_resources").paginate(
            StackName=stack_name
        ) for item in page["StackResourceSummaries"]
    ]
    ids = {item["ResourceType"]: item["PhysicalResourceId"] for item in resources
           if item["ResourceType"] in {"AWS::RDS::DBInstance", "AWS::KMS::Key"}}
    return ids.get("AWS::RDS::DBInstance"), ids.get("AWS::KMS::Key")


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


def teardown_stack(environment: str, region: str, account_id: str, session: boto3.Session) -> None:
    stack_name = f"AgentsPlatform-{environment}"
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
    delete_managed_apps(cloudformation, session.client("ecr"), environment)
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


def remove_staging_and_bootstrap(region: str, account_id: str, session: boto3.Session) -> None:
    cloudformation = session.client("cloudformation")
    names = active_stacks(cloudformation)
    app_names = {name for name, _ in managed_app_stacks(cloudformation, "staging")}
    other = names - {STACK_NAME, BOOTSTRAP_STACK} - app_names
    if other:
        raise ValueError("Other active stacks exist: " + ", ".join(sorted(other)))
    if BOOTSTRAP_STACK not in names:
        raise ValueError("CDKToolkit stack was not found")
    _, bucket, repository = bootstrap_assets(cloudformation, account_id, region)
    print(f"Staging stack: {STACK_NAME if STACK_NAME in names else 'already absent'}")
    print(f"CDK bootstrap: {BOOTSTRAP_STACK}; S3: {bucket}; ECR: {repository}")
    phrase = f"DELETE STAGING AND CDK {account_id} {region}"
    if input(f"Type '{phrase}' to proceed: ").strip() != phrase:
        raise ValueError("Cleanup cancelled: confirmation did not match")
    if STACK_NAME in names:
        teardown_stack("staging", region, account_id, session)
    else:
        delete_managed_apps(cloudformation, session.client("ecr"), "staging")
    require_exclusive_bootstrap(cloudformation)
    delete_bootstrap(session, account_id, region)


def skipped_resources(path: Path, account_id: str, region: str) -> dict[str, dict]:
    events = json.loads(path.read_text())
    prefix = f"arn:aws:cloudformation:{region}:{account_id}:stack/{STACK_NAME}/"
    if not isinstance(events, list) or not events or any(
        event.get("StackName") != STACK_NAME or not event.get("StackId", "").startswith(prefix)
        for event in events
    ):
        raise ValueError("Deletion report does not match this stack, account, and Region")
    latest = {event["LogicalResourceId"]: event
              for event in sorted(events, key=lambda item: item["Timestamp"])}
    stack_event = latest.get(STACK_NAME)
    if not stack_event or stack_event["ResourceStatus"] != "DELETE_COMPLETE":
        raise ValueError("Deletion report does not show a completed stack deletion")
    return {event["ResourceType"]: event for event in latest.values()
            if event["ResourceStatus"] == "DELETE_SKIPPED"
            and event["ResourceType"] in {"AWS::RDS::DBInstance", "AWS::KMS::Key"}}


def matching_bootstrap_bucket(s3: object, account_id: str, region: str) -> str | None:
    suffix = f"-assets-{account_id}-{region}"
    buckets = [item["Name"] for item in s3.list_buckets()["Buckets"]
               if item["Name"].startswith("cdk-") and item["Name"].endswith(suffix)]
    if len(buckets) > 1:
        raise ValueError("Multiple CDK asset buckets match this account and Region")
    return buckets[0] if buckets else None


def oidc_role_users(iam: object, provider_arn: str) -> list[str]:
    return [
        role["RoleName"]
        for page in iam.get_paginator("list_roles").paginate()
        for role in page["Roles"]
        if provider_arn in json.dumps(role.get("AssumeRolePolicyDocument", {}))
    ]


def staging_role(iam: object) -> bool:
    try:
        iam.get_role(RoleName=ROLE_NAME)
        return True
    except iam.exceptions.NoSuchEntityException:
        return False


def verify_staging_role(iam: object) -> bool:
    if not staging_role(iam):
        return False
    policies = iam.list_role_policies(RoleName=ROLE_NAME)["PolicyNames"]
    attached = iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]
    profiles = iam.list_instance_profiles_for_role(RoleName=ROLE_NAME)["InstanceProfiles"]
    if set(policies) - {"deploy"} or attached or profiles:
        raise ValueError("Staging role has unexpected policies or instance profiles; review it manually")
    return True


def delete_staging_identity(iam: object, account_id: str) -> None:
    provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/token.actions.githubusercontent.com"
    users = oidc_role_users(iam, provider_arn)
    other_users = set(users) - {ROLE_NAME}
    if other_users:
        raise ValueError("OIDC provider is trusted by other roles: " + ", ".join(sorted(other_users)))
    if verify_staging_role(iam):
        policies = iam.list_role_policies(RoleName=ROLE_NAME)["PolicyNames"]
        if "deploy" in policies:
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="deploy")
        iam.delete_role(RoleName=ROLE_NAME)
    try:
        iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn)
    except iam.exceptions.NoSuchEntityException:
        return
    iam.delete_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn)


def execute(path: Path | None, account_id: str, region: str, *, delete: bool = True,
            db_id: str | None = None, key_id: str | None = None) -> None:
    if len(account_id) != 12 or not account_id.isdigit():
        raise ValueError("--account-id must be a 12-digit AWS account ID")
    skipped = skipped_resources(path, account_id, region) if path else {}
    session = aws_session(region)
    actual_account = session.client("sts").get_caller_identity()["Account"]
    if actual_account != account_id:
        raise ValueError(f"AWS account mismatch: expected {account_id}, got {actual_account}")
    cloudformation = session.client("cloudformation")
    active = active_stacks(cloudformation)
    if active:
        raise ValueError("Active CloudFormation stacks exist; bootstrap assets may be shared: "
                         + ", ".join(sorted(active)))
    s3 = session.client("s3")
    rds = session.client("rds")
    iam = session.client("iam")
    kms = session.client("kms")
    bucket = matching_bootstrap_bucket(s3, account_id, region)
    db = db_id or skipped.get("AWS::RDS::DBInstance", {}).get("PhysicalResourceId")
    key = key_id or skipped.get("AWS::KMS::Key", {}).get("PhysicalResourceId")
    manual = []
    automated = []
    if db:
        manual = [snapshot for page in rds.get_paginator("describe_db_snapshots").paginate(
            DBInstanceIdentifier=db, SnapshotType="manual"
        ) for snapshot in page["DBSnapshots"]]
        automated = [backup for page in rds.get_paginator(
            "describe_db_instance_automated_backups"
        ).paginate(DBInstanceIdentifier=db) for backup in page["DBInstanceAutomatedBackups"]]
    provider_arn = f"arn:aws:iam::{account_id}:oidc-provider/token.actions.githubusercontent.com"
    other_users = set(oidc_role_users(iam, provider_arn)) - {ROLE_NAME}
    if other_users:
        raise ValueError("OIDC provider is trusted by other roles: " + ", ".join(sorted(other_users)))
    has_role = verify_staging_role(iam)
    key_state = "not in report"
    if key:
        try:
            key_state = kms.describe_key(KeyId=key)["KeyMetadata"]["KeyState"]
        except kms.exceptions.NotFoundException:
            key_state = "Absent"
        if key_state not in {"Enabled", "Disabled", "PendingDeletion", "Absent"}:
            raise ValueError(f"Unexpected KMS key state: {key_state}")
    print(f"CDK asset bucket: {bucket or 'absent'}")
    print("Manual DB snapshots:", [item["DBSnapshotIdentifier"] for item in manual])
    print("Retained automated backups:", [item["DbiResourceId"] for item in automated])
    print(f"Staging GitHub role: {'present' if has_role else 'absent'}")
    try:
        iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn)
        provider_status = "present"
    except iam.exceptions.NoSuchEntityException:
        provider_status = "absent"
    print(f"GitHub OIDC provider: {provider_status}")
    print(f"Queue KMS key: {key or 'not in report'} ({key_state})")
    if not delete:
        print("Read-only inventory complete. No AWS changes were made.")
        return
    phrase = f"DELETE STAGING LEFTOVERS {account_id} {region}"
    if input(f"Type '{phrase}' to permanently remove the listed data and identity: ").strip() != phrase:
        raise ValueError("Cleanup cancelled: confirmation did not match")

    for snapshot in manual:
        rds.delete_db_snapshot(DBSnapshotIdentifier=snapshot["DBSnapshotIdentifier"])
    for backup in automated:
        rds.delete_db_instance_automated_backup(DbiResourceId=backup["DbiResourceId"])
    if bucket:
        empty_bucket(s3, bucket)
        s3.delete_bucket(Bucket=bucket)
    delete_staging_identity(iam, account_id)
    if key and key_state in {"Enabled", "Disabled"}:
        kms.schedule_key_deletion(KeyId=key, PendingWindowInDays=7)
    print("Cleanup requests submitted. Verify snapshot deletion and KMS pending-deletion status in AWS.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stack", "all", "leftovers"), default="all")
    parser.add_argument("--environment", choices=("staging", "production"), default="staging")
    parser.add_argument("--region", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Read-only live AWS inventory")
    action.add_argument("--execute", action="store_true", help="Delete after confirmation")
    args = parser.parse_args()
    try:
        if args.environment == "production" and args.mode != "stack":
            raise ValueError("Production cleanup supports --mode stack only")
        if args.mode == "leftovers":
            skipped_resources(args.report, args.account_id, args.region)
        if not args.execute and not args.check:
            print(f"Plan only: {args.mode} cleanup for {args.environment} in {args.account_id}/{args.region}.")
            print("Add --check for a read-only AWS inventory or --execute for confirmed deletion.")
            print("No AWS calls were made.")
            return 0
        if args.mode == "leftovers":
            execute(args.report, args.account_id, args.region, delete=args.execute)
            return 0
        if len(args.account_id) != 12 or not args.account_id.isdigit():
            raise ValueError("--account-id must be a 12-digit AWS account ID")
        session = aws_session(args.region)
        actual = session.client("sts").get_caller_identity()["Account"]
        if actual != args.account_id:
            raise ValueError(f"AWS account mismatch: expected {args.account_id}, got {actual}")
        cloudformation = session.client("cloudformation")
        names = active_stacks(cloudformation)
        stack_name = f"AgentsPlatform-{args.environment}"
        app_names = {name for name, _ in managed_app_stacks(cloudformation, args.environment)}
        if args.check:
            print(f"Application stack: {'present' if stack_name in names else 'absent'}")
            print(f"Managed application stacks: {', '.join(sorted(app_names)) or 'absent'}")
            if args.mode == "all":
                print(f"CDK bootstrap: {'present' if BOOTSTRAP_STACK in names else 'absent'}")
                if not names and args.report.exists():
                    execute(args.report, args.account_id, args.region, delete=False)
                elif not names:
                    print("No deletion report found; old snapshots cannot be checked.")
            if not (args.mode == "all" and not names and args.report.exists()):
                print("Read-only check complete. No AWS changes were made.")
            return 0
        if args.mode == "stack":
            if stack_name in names:
                teardown_stack(args.environment, args.region, args.account_id, session)
            else:
                print(f"{stack_name} is already absent.")
                delete_managed_apps(cloudformation, session.client("ecr"), args.environment)
            return 0
        other_stacks = names - {STACK_NAME, BOOTSTRAP_STACK} - app_names
        if other_stacks:
            raise ValueError("CDK bootstrap may be shared; other active stacks exist: "
                             + ", ".join(sorted(other_stacks)))
        if STACK_NAME not in names and not args.report.exists():
            raise ValueError("A deletion report is required when the staging stack is already absent")
        if BOOTSTRAP_STACK in names:
            db_id, key_id = (app_resource_ids(cloudformation, STACK_NAME)
                             if STACK_NAME in names else (None, None))
            remove_staging_and_bootstrap(args.region, args.account_id, session)
        elif STACK_NAME in names:
            db_id, key_id = app_resource_ids(cloudformation, STACK_NAME)
            teardown_stack("staging", args.region, args.account_id, session)
        else:
            db_id, key_id = None, None
        if not (db_id or args.report.exists()):
            raise ValueError("No live database ID or deletion report; cannot verify snapshot cleanup")
        execute(args.report if args.report.exists() and not db_id else None,
                args.account_id, args.region, delete=True, db_id=db_id, key_id=key_id)
        print("Staging cleanup complete. KMS keys may remain pending deletion.")
        return 0
    except (ClientError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
