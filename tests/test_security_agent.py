import json
import subprocess
from pathlib import Path

import pytest

from agents.security_agent import SecurityAgent, SecurityFindingsError
from agents_platform.worker import run_once
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import (
    AgentKind,
    TaskStatus,
    WorkflowTask,
    WorkflowTaskMessage,
)


def test_clean_scan_returns_structured_report(tmp_path: Path) -> None:
    agent = SecurityAgent(tmp_path, scanners=("bandit", "trivy"))

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        report = {"results": []} if command[0] == "bandit" else {"Results": []}
        return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

    agent._run = fake_run
    output = json.loads(agent.execute(WorkflowTask(agent=AgentKind.SECURITY, instruction="scan")))
    assert output == {"outcome": "clean", "findings": [], "scanners": ["bandit", "trivy"]}


def test_bandit_report_uses_repository_relative_paths(tmp_path: Path) -> None:
    file = tmp_path / "app.py"
    file.write_text("print('ready')\n")
    agent = SecurityAgent(tmp_path, scanners=("bandit",))
    agent._run = lambda command: subprocess.CompletedProcess(
        command, 1,
        json.dumps({"results": [{
            "filename": str(file), "test_id": "B104", "line_number": 1,
            "issue_severity": "MEDIUM", "issue_text": "Possible binding to all interfaces.",
        }]}), "",
    )
    with pytest.raises(SecurityFindingsError) as error:
        agent.execute(WorkflowTask(agent=AgentKind.SECURITY, instruction="scan"))
    report = json.loads(str(error.value))
    assert report["findings"][0]["file"] == "app.py"


def test_findings_fail_worker_without_exposing_secret(tmp_path: Path) -> None:
    agent = SecurityAgent(tmp_path, scanners=("gitleaks",))

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        report_path = Path(command[command.index("--report-path") + 1])
        report_path.write_text(
            json.dumps([{
                "RuleID": "generic-api-key", "File": "config.py", "StartLine": 4,
                "Description": "API key", "Secret": "do-not-leak-this-value",
            }])
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    agent._run = fake_run
    broker = InMemoryTaskBroker()
    task = WorkflowTask(agent=AgentKind.SECURITY, instruction="scan")
    from uuid import uuid4

    broker.publish(WorkflowTaskMessage(workflow_id=uuid4(), task=task))
    assert run_once(broker, AgentKind.SECURITY, agent.execute)
    result = broker.receive_result()
    assert result is not None
    assert result.status == TaskStatus.FAILED
    assert "do-not-leak-this-value" not in result.output
    assert json.loads(result.output)["findings"][0]["rule"] == "generic-api-key"
