import subprocess
from pathlib import Path

import pytest

from agents_platform.source import checkout_source
from agents_shared.models import GitHubSourceTarget


def test_checkout_reports_missing_branch_without_exposing_git_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 128, "", "warning: token=private-value\nfatal: Remote branch main not found in upstream origin"
        ),
    )
    target = GitHubSourceTarget(owner="karitselmuthu", repo="Multilingual_Call_Intelligent_Agent", ref="main")
    with pytest.raises(RuntimeError, match="Branch 'main' does not exist") as error:
        checkout_source(target, tmp_path / "source")
    assert "private-value" not in str(error.value)


def test_checkout_reports_network_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 128, "", "fatal: Could not resolve host: github.com"
        ),
    )
    with pytest.raises(RuntimeError, match="Cannot reach GitHub"):
        checkout_source(GitHubSourceTarget(owner="acme", repo="app", ref="main"), tmp_path / "source")
