import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts import remove_staging_aws
sys.path.pop(0)


def test_plan_mode_does_not_contact_aws(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", [
        "remove_staging_aws", "--region", "us-east-1", "--account-id", "123456789012",
    ])
    monkeypatch.setattr(remove_staging_aws.boto3, "Session", lambda **kwargs: pytest.fail("AWS contacted"))
    assert remove_staging_aws.main() == 0
    assert "No AWS calls were made" in capsys.readouterr().out


def test_shared_bootstrap_is_refused() -> None:
    client = SimpleNamespace(get_paginator=lambda name: SimpleNamespace(paginate=lambda: [{
        "StackSummaries": [
            {"StackName": "CDKToolkit", "StackStatus": "CREATE_COMPLETE"},
            {"StackName": "AnotherApplication", "StackStatus": "CREATE_COMPLETE"},
        ],
    }]))
    with pytest.raises(ValueError, match="other active stacks"):
        remove_staging_aws.require_exclusive_bootstrap(client)


def test_bootstrap_assets_must_match_account_and_region() -> None:
    resources = [
        {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "cdk-hnb659fds-assets-123456789012-us-east-1"},
        {"ResourceType": "AWS::ECR::Repository", "PhysicalResourceId": "cdk-hnb659fds-container-assets-123456789012-us-east-1"},
    ]
    client = SimpleNamespace(
        describe_stacks=lambda **kwargs: {"Stacks": [{"StackId": "stack-id"}]},
        get_paginator=lambda name: SimpleNamespace(paginate=lambda **kwargs: [{"StackResourceSummaries": resources}]),
    )
    assert remove_staging_aws.bootstrap_assets(client, "123456789012", "us-east-1")[1:] == (
        resources[0]["PhysicalResourceId"], resources[1]["PhysicalResourceId"],
    )
    with pytest.raises(ValueError, match="do not match"):
        remove_staging_aws.bootstrap_assets(client, "999999999999", "us-east-1")
