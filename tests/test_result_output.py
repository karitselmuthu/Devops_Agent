import json
from uuid import uuid4

from agents_platform.worker import run_once
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import AgentKind, TaskStatus, WorkflowTask, WorkflowTaskMessage
from agents_shared.results import MAX_RESULT_LENGTH, bounded_result_output


def test_large_scan_report_still_records_a_valid_result() -> None:
    report = {
        "outcome": "blocked",
        "findings": [{"file": f"file-{index}.py", "message": "A" * 120} for index in range(300)],
        "scanners": ["bandit", "trivy", "gitleaks"],
    }
    broker = InMemoryTaskBroker()
    task = WorkflowTask(agent=AgentKind.SECURITY, instruction="scan")
    broker.publish(WorkflowTaskMessage(workflow_id=uuid4(), task=task))

    def fail(_task: WorkflowTask) -> str:
        raise RuntimeError(json.dumps(report))

    assert run_once(broker, AgentKind.SECURITY, fail)
    result = broker.receive_result()
    assert result is not None
    assert result.status == TaskStatus.FAILED
    assert len(result.output) <= MAX_RESULT_LENGTH
    summary = json.loads(result.output)
    assert summary["outcome"] == "blocked"
    assert summary["scanners"] == ["bandit", "trivy", "gitleaks"]
    assert summary["total_findings"] == 300
    assert summary["truncated"] is True
    assert 0 < len(summary["findings"]) < 300


def test_large_plain_output_is_bounded() -> None:
    output = bounded_result_output("x" * 30_000)
    assert len(output) == MAX_RESULT_LENGTH
    assert output.endswith("[output truncated]")
