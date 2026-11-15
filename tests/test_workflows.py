from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agents_platform.api import create_app
from agents_platform.orchestrator import InvalidTaskResult, Orchestrator
from agents_platform.worker import run_once
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import AgentKind, GitHubSourceTarget, TaskResultMessage, TaskStatus, WorkflowRequest


def source() -> GitHubSourceTarget:
    return GitHubSourceTarget(owner="acme", repo="billing", ref="main")


def test_workflow_advances_one_agent_at_a_time() -> None:
    broker = InMemoryTaskBroker()
    orchestrator = Orchestrator(broker)
    plan = orchestrator.create(
        WorkflowRequest(request="Scan release", service="billing", environment="staging", source_target=source())
    )

    assert [task.agent for task in plan.tasks] == [AgentKind.SECURITY]
    assert [task.status for task in plan.tasks] == [TaskStatus.QUEUED]

    for agent in [AgentKind.SECURITY]:
        message = broker.receive(agent)
        assert message is not None
        assert message.workflow_id == plan.id
        assert broker.receive(agent) is None
        orchestrator.record_result(
            TaskResultMessage(
                workflow_id=plan.id,
                task_id=message.task.id,
                status=TaskStatus.COMPLETED,
                output=f"{agent} done",
            )
        )

    finished = orchestrator.get(plan.id)
    assert all(task.status == TaskStatus.COMPLETED for task in finished.tasks)
    assert finished.tasks[-1].output == "SECURITY done"


def test_failed_task_stops_workflow_and_duplicate_result_is_rejected() -> None:
    broker = InMemoryTaskBroker()
    orchestrator = Orchestrator(broker)
    plan = orchestrator.create(WorkflowRequest(request="Propose", service="api", environment="dev", source_target=source(), code_change=True))
    message = broker.receive(AgentKind.CODE)
    assert message is not None
    result = TaskResultMessage(
        workflow_id=plan.id, task_id=message.task.id, status=TaskStatus.FAILED, output="Build failed"
    )
    orchestrator.record_result(result)
    assert orchestrator.get(plan.id).tasks[0].status == TaskStatus.FAILED
    with pytest.raises(InvalidTaskResult):
        orchestrator.record_result(result)


def test_api_validates_and_returns_workflows() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/workflows",
        json={"request": "Scan release", "service": "billing", "environment": "staging", "source_target": source().model_dump()},
    )
    assert response.status_code == 201
    workflow_id = response.json()["id"]
    assert client.get(f"/workflows/{workflow_id}").status_code == 200
    assert client.post(
        "/workflows", json={"request": "", "service": "billing", "environment": "staging"}
    ).status_code == 422
    assert client.post(
        "/workflows", json={"request": "Deploy", "service": "billing", "environment": "staging"}
    ).status_code == 422
    assert client.get(f"/workflows/{uuid4()}").status_code == 404
    assert len(client.get("/integrations/tools").json()) == 4


def test_worker_result_returns_through_result_queue() -> None:
    broker = InMemoryTaskBroker()
    orchestrator = Orchestrator(broker)
    plan = orchestrator.create(WorkflowRequest(request="Propose", service="api", environment="dev", source_target=source(), code_change=True))
    assert run_once(broker, AgentKind.CODE, lambda task: f"handled {task.agent}")
    updated = orchestrator.process_next_result()
    assert updated is not None
    assert updated.tasks[0].output == "handled CODE"
    assert len(updated.tasks) == 1
