from pathlib import Path
from runpy import run_path

import pytest


deployment_counts = run_path(
    str(Path(__file__).parents[1] / "scripts" / "deploy_aws.py")
)["deployment_counts"]
previous_counts = run_path(
    str(Path(__file__).parents[1] / "scripts" / "deploy_aws.py")
)["previous_counts"]


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
