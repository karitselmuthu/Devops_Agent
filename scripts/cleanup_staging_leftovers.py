"""Clean up staging resources orphaned by a completed CloudFormation deletion.

The default mode reads the deletion report only. --execute is destructive.
"""

import argparse
import json
from pathlib import Path
import sys

from botocore.exceptions import ClientError

from scripts.remove_staging_aws import active_stacks, empty_bucket
from scripts.teardown_aws import aws_session


STACK_NAME = "AgentsPlatform-staging"
ROLE_NAME = "DevopsAgentGitHubStaging"
DEFAULT_REPORT = Path("AgentsPlatform-staging-deletion-report.json")


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


def execute(path: Path, account_id: str, region: str, *, delete: bool = True) -> None:
    if len(account_id) != 12 or not account_id.isdigit():
        raise ValueError("--account-id must be a 12-digit AWS account ID")
    skipped = skipped_resources(path, account_id, region)
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
    db = skipped.get("AWS::RDS::DBInstance", {}).get("PhysicalResourceId")
    key = skipped.get("AWS::KMS::Key", {}).get("PhysicalResourceId")
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
    parser.add_argument("--region", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only live AWS inventory")
    mode.add_argument("--execute", action="store_true", help="Delete listed leftovers after confirmation")
    args = parser.parse_args()
    try:
        skipped = skipped_resources(args.report, args.account_id, args.region)
        if not args.execute and not args.check:
            print(f"Plan only: inspect post-deletion leftovers for {STACK_NAME} in {args.account_id}/{args.region}.")
            print("Report contains", len(skipped), "RDS/KMS entries to check.")
            print("Execution checks live AWS state, requires typed confirmation, and deletes only listed leftovers.")
            print("No AWS calls were made. Add --execute when ready.")
            return 0
        execute(args.report, args.account_id, args.region, delete=args.execute)
        return 0
    except (ClientError, OSError, ValueError, RuntimeError) as exc:
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
