from pathlib import Path
from runpy import run_path
import sys
import json
import subprocess

import pytest


teardown = run_path(str(Path(__file__).parents[1] / "scripts" / "teardown_aws.py"))


def test_plan_mode_never_contacts_aws(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", [
        "teardown_aws.py", "--environment", "staging", "--region", "us-east-1",
        "--account-id", "123456789012",
    ])
    monkeypatch.setattr(teardown["boto3"], "Session", lambda **kwargs: pytest.fail("AWS contacted"))
    assert teardown["main"]() == 0
    assert "No AWS calls were made" in capsys.readouterr().out


def test_teardown_parameters_zero_services_and_preserve_current_settings() -> None:
    stack = {"Parameters": [
        {"ParameterKey": "CertificateArn", "ParameterValue": "arn:aws:acm:us-east-1:123456789012:certificate/id"},
        {"ParameterKey": "DomainName", "ParameterValue": "api.example.com"},
        {"ParameterKey": "HostedZoneId", "ParameterValue": "Z123"},
        {"ParameterKey": "ImageTag", "ParameterValue": "release"},
        {"ParameterKey": "DesiredCount", "ParameterValue": "1"},
    ]}
    arguments = teardown["parameter_arguments"](stack)
    assert "ApiDesiredCount=0" in arguments
    assert "BackgroundDesiredCount=0" in arguments
    assert "ImageTag=release" in arguments
    assert not any("DesiredCount=1" in argument for argument in arguments)


def test_legacy_http_stack_refuses_destructive_update() -> None:
    with pytest.raises(ValueError, match="predates the HTTPS template"):
        teardown["parameter_arguments"]({"Parameters": []})


def test_aws_cli_login_fallback_keeps_credentials_in_memory(monkeypatch) -> None:
    seen = []

    def session(**kwargs):
        seen.append(kwargs)
        if "aws_access_key_id" not in kwargs:
            return type("MissingCrtSession", (), {
                "get_credentials": lambda self: (_ for _ in ()).throw(
                    teardown["MissingDependencyException"](msg="CRT missing")
                )
            })()
        return kwargs

    monkeypatch.setattr(teardown["boto3"], "Session", session)
    monkeypatch.setattr(teardown["subprocess"], "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, stdout=json.dumps({
            "AccessKeyId": "temporary-id", "SecretAccessKey": "temporary-secret",
            "SessionToken": "temporary-token",
        })
    ))
    result = teardown["aws_session"]("us-east-1")
    assert result["aws_access_key_id"] == "temporary-id"
    assert result["aws_session_token"] == "temporary-token"
    assert seen[0] == {"region_name": "us-east-1"}
