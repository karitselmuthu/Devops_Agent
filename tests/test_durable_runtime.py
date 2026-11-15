import os
import subprocess
from pathlib import Path

from agents_platform.persistence import DurableWorkflowStore
from agents_platform.runtime import agent_once, dispatch_once, result_once
from agents_shared.models import AgentKind, GitHubSourceTarget, TaskStatus, WorkflowRequest
from agents_shared.sqs import Delivery


class FakeBroker:
    def __init__(self) -> None:
        self.tasks = []
        self.results = []
        self.task_acks = 0
        self.result_acks = 0

    def publish(self, message) -> None:
        self.tasks.append(message)

    def publish_result(self, result) -> None:
        self.results.append(result)

    def receive_task_delivery(self, agent):
        if not self.tasks or self.tasks[0].task.agent != agent:
            return None
        message = self.tasks.pop(0)
        return Delivery(message, lambda: self._ack_task(), lambda: None)

    def receive_result_delivery(self):
        if not self.results:
            return None
        result = self.results.pop(0)
        return Delivery(result, lambda: self._ack_result(), lambda: None)

    def _ack_task(self) -> None:
        self.task_acks += 1

    def _ack_result(self) -> None:
        self.result_acks += 1


def test_durable_dispatch_worker_result_path(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'app.db'}"
    env = dict(os.environ, DATABASE_URL=database_url)
    subprocess.run(
        [".venv/bin/alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1], env=env, check=True, capture_output=True,
    )
    store = DurableWorkflowStore(database_url)
    broker = FakeBroker()
    plan = store.create(WorkflowRequest(
        request="scan then release", service="api", environment="staging",
        source_target=GitHubSourceTarget(owner="acme", repo="api", ref="main"),
        devops_target={"provider": "aws_codepipeline", "name": "api", "region": "us-east-1"},
    ))
    assert dispatch_once(store, broker) == 1
    assert agent_once(store, broker, AgentKind.SECURITY, lambda task: "scan clean")
    assert broker.task_acks == 1
    assert result_once(store, broker)
    assert broker.result_acks == 1
    assert store.get(plan.id).tasks[0].status == TaskStatus.COMPLETED
    assert dispatch_once(store, broker) == 1
    assert broker.tasks[0].task.agent == AgentKind.DEVOPS
