import json
from uuid import uuid4

import httpx
import pytest

from agents.devops_agent import CodePipelineProvider, DevOpsAgent, GitHubActionsProvider, RunState
from agents_shared.models import (
    AWSCodePipelineTarget,
    AgentKind,
    GitHubActionsTarget,
    WorkflowRequest,
    WorkflowTask,
)
from planning_agent.planner import create_plan


def test_github_dispatch_and_status() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"workflow_run_id": 123})
        return httpx.Response(200, json={"status": "completed", "conclusion": "success"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = GitHubActionsProvider("token", client)
    target = GitHubActionsTarget(
        owner="acme", repo="app", workflow_id="deploy.yml", ref="main", inputs={"env": "dev"}
    )
    assert provider.start(target, uuid4()) == "123"
    assert json.loads(requests[0].content)["return_run_details"] is True
    assert provider.status(target, "123") == RunState.SUCCEEDED
    assert requests[1].url.path.endswith("/actions/runs/123")


class FakeCodePipelineClient:
    def __init__(self) -> None:
        self.start_args: dict | None = None

    def start_pipeline_execution(self, **kwargs: str) -> dict[str, str]:
        self.start_args = kwargs
        return {"pipelineExecutionId": "execution-123"}

    def get_pipeline_execution(self, **kwargs: str) -> dict:
        return {"pipelineExecution": {"status": "Succeeded"}}


def test_codepipeline_uses_stable_task_id_and_waits_for_success() -> None:
    client = FakeCodePipelineClient()
    provider = CodePipelineProvider({"us-east-1": client})
    target = AWSCodePipelineTarget(name="deploy-api", region="us-east-1")
    task = WorkflowTask(agent=AgentKind.DEVOPS, instruction="deploy", devops_target=target)
    output = json.loads(DevOpsAgent(codepipeline=provider, poll_interval=0.01).execute(task))
    assert client.start_args == {"name": "deploy-api", "clientRequestToken": str(task.id)}
    assert output == {
        "provider": "aws_codepipeline", "execution_id": "execution-123", "status": "SUCCEEDED"
    }


def test_plan_carries_devops_target_and_missing_target_fails() -> None:
    target = GitHubActionsTarget(owner="acme", repo="app", workflow_id="ci.yml", ref="main")
    plan = create_plan(
        WorkflowRequest(request="deploy", service="api", environment="prod", devops_target=target)
    )
    assert plan.tasks[0].devops_target == target
    assert len(plan.tasks) == 1
    with pytest.raises(ValueError, match="requires a CI/CD target"):
        DevOpsAgent().execute(WorkflowTask(agent=AgentKind.DEVOPS, instruction="deploy"))
