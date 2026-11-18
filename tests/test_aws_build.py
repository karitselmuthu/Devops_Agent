from pathlib import Path
from runpy import run_path
import base64
import sys

import pytest


build = run_path(
    str(Path(__file__).parents[1] / "scripts" / "aws_build.py")
)
deployment_counts = build["deployment_counts"]
previous_counts = build["previous_counts"]
endpoint_parameters = build["endpoint_parameters"]


def test_deployment_counts_keep_production_api_redundant(monkeypatch) -> None:
    monkeypatch.delenv("BACKGROUND_DESIRED_COUNT", raising=False)
    assert deployment_counts("staging") == (1, 1)
    assert deployment_counts("production") == (2, 1)

    monkeypatch.setenv("BACKGROUND_DESIRED_COUNT", "0")
    assert deployment_counts("staging") == (1, 0)
    with pytest.raises(ValueError, match="Production requires"):
        deployment_counts("production")

    monkeypatch.setenv("BACKGROUND_DESIRED_COUNT", "2")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        deployment_counts("staging")


def test_previous_counts_preserve_legacy_service_count() -> None:
    assert previous_counts({"Parameters": [{"ParameterKey": "DesiredCount", "ParameterValue": "1"}]}) == ("1", "1")
    assert previous_counts({"Parameters": [
        {"ParameterKey": "ApiDesiredCount", "ParameterValue": "2"},
        {"ParameterKey": "BackgroundDesiredCount", "ParameterValue": "0"},
    ]}) == ("2", "0")


def test_staging_uses_managed_https_without_domain_settings(monkeypatch) -> None:
    monkeypatch.setenv("CLOUDFRONT_PREFIX_LIST_ID", "pl-3b927c52")
    monkeypatch.delenv("CERTIFICATE_ARN", raising=False)
    monkeypatch.delenv("API_DOMAIN", raising=False)
    monkeypatch.delenv("HOSTED_ZONE_ID", raising=False)
    assert endpoint_parameters("staging") == {"CloudFrontPrefixListId": "pl-3b927c52"}
    with pytest.raises(ValueError, match="CERTIFICATE_ARN"):
        endpoint_parameters("production")


def test_setup_plan_makes_no_external_calls(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", [
        "aws_build.py", "setup", "--region", "us-east-1", "--environment", "staging",
    ])
    monkeypatch.setattr(build["subprocess"], "run", lambda *args, **kwargs: pytest.fail("external call"))
    assert build["main"]() == 0
    assert "No external calls were made" in capsys.readouterr().out


def test_ecr_login_passes_password_only_through_stdin(monkeypatch) -> None:
    encoded = base64.b64encode(b"AWS:temporary-password").decode()
    client = type("Ecr", (), {"get_authorization_token": lambda self: {
        "authorizationData": [{"authorizationToken": encoded, "proxyEndpoint": "https://registry.example"}]
    }})()
    session = type("Session", (), {"client": lambda self, name: client})()
    calls = []
    monkeypatch.setattr(build["subprocess"], "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    build["login_ecr"](session)
    assert calls[0][0][0][:4] == ["docker", "login", "--username", "AWS"]
    assert calls[0][1]["input"] == "temporary-password"
    assert "temporary-password" not in calls[0][0][0]


def test_setup_refuses_orphaned_cdk_bucket_before_creating_resources(monkeypatch) -> None:
    responses = iter([
        "123456789012",
        '{"owner":{"login":"owner","id":1},"name":"repo","id":2}',
        '{"StackSummaries":[]}',
        '{"Buckets":[{"Name":"cdk-hnb659fds-assets-123456789012-us-east-1"}]}',
    ])
    globals_for_setup = build["setup_access"].__globals__
    monkeypatch.setitem(globals_for_setup, "output_text", lambda *args: next(responses))
    monkeypatch.setitem(globals_for_setup, "run", lambda *args: pytest.fail("resource created"))
    with pytest.raises(ValueError, match="asset bucket remains"):
        build["setup_access"]("us-east-1", "staging")
