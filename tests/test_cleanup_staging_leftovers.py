import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import cleanup_staging_leftovers as cleanup
sys.path.pop(0)


def report(path: Path) -> None:
    stack_id = "arn:aws:cloudformation:us-east-1:123456789012:stack/AgentsPlatform-staging/id"
    path.write_text(json.dumps([
        {"StackId": stack_id, "StackName": "AgentsPlatform-staging",
         "LogicalResourceId": "Database", "ResourceType": "AWS::RDS::DBInstance",
         "PhysicalResourceId": "staging-db", "ResourceStatus": "DELETE_SKIPPED",
         "Timestamp": "2026-11-16T00:00:00Z"},
        {"StackId": stack_id, "StackName": "AgentsPlatform-staging",
         "LogicalResourceId": "AgentsPlatform-staging", "ResourceType": "AWS::CloudFormation::Stack",
         "ResourceStatus": "DELETE_COMPLETE", "Timestamp": "2026-11-16T01:00:00Z"},
    ]))


def test_report_rejects_wrong_account(tmp_path) -> None:
    path = tmp_path / "report.json"
    report(path)
    assert cleanup.skipped_resources(path, "123456789012", "us-east-1")[
        "AWS::RDS::DBInstance"
    ]["PhysicalResourceId"] == "staging-db"
    with pytest.raises(ValueError, match="does not match"):
        cleanup.skipped_resources(path, "999999999999", "us-east-1")


def test_plan_mode_never_contacts_aws(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "report.json"
    report(path)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_staging_leftovers", "--region", "us-east-1",
        "--account-id", "123456789012", "--report", str(path),
    ])
    monkeypatch.setattr(cleanup, "aws_session", lambda region: pytest.fail("AWS contacted"))
    assert cleanup.main() == 0
    assert "No AWS calls were made" in capsys.readouterr().out


def test_check_mode_is_read_only(tmp_path, monkeypatch) -> None:
    path = tmp_path / "report.json"
    report(path)
    monkeypatch.setattr(sys, "argv", [
        "cleanup_staging_leftovers", "--region", "us-east-1",
        "--account-id", "123456789012", "--report", str(path), "--check",
    ])
    calls = []
    monkeypatch.setattr(cleanup, "execute", lambda *args, **kwargs: calls.append(kwargs["delete"]))
    assert cleanup.main() == 0
    assert calls == [False]


def test_bootstrap_bucket_match_is_specific() -> None:
    s3 = SimpleNamespace(list_buckets=lambda: {"Buckets": [
        {"Name": "cdk-hnb659fds-assets-123456789012-us-east-1"},
        {"Name": "other-bucket"},
    ]})
    assert cleanup.matching_bootstrap_bucket(s3, "123456789012", "us-east-1") == (
        "cdk-hnb659fds-assets-123456789012-us-east-1"
    )
    assert cleanup.matching_bootstrap_bucket(s3, "123456789012", "us-west-2") is None
