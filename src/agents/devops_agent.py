"""CI/CD orchestration for GitHub Actions and AWS CodePipeline."""

import json
import time
from enum import StrEnum
from uuid import UUID

import httpx

from agents_shared.models import (
    AWSCodePipelineTarget,
    AgentKind,
    GitHubActionsTarget,
    WorkflowTask,
)


class RunState(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class GitHubActionsProvider:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self.client = client or httpx.Client(timeout=20)
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        }

    def start(self, target: GitHubActionsTarget, task_id: UUID) -> str:
        url = (
            f"https://api.github.com/repos/{target.owner}/{target.repo}/actions/"
            f"workflows/{target.workflow_id}/dispatches"
        )
        response = self.client.post(
            url,
            headers=self.headers,
            json={"ref": target.ref, "inputs": target.inputs, "return_run_details": True},
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError("GitHub did not return workflow run details")
        run_id = response.json().get("workflow_run_id")
        if not isinstance(run_id, int):
            raise RuntimeError("GitHub response did not include a workflow run ID")
        return str(run_id)

    def status(self, target: GitHubActionsTarget, execution_id: str) -> RunState:
        response = self.client.get(
            f"https://api.github.com/repos/{target.owner}/{target.repo}/actions/runs/{execution_id}",
            headers=self.headers,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}:
            return RunState.RUNNING
        if data.get("status") == "completed":
            return RunState.SUCCEEDED if data.get("conclusion") == "success" else RunState.FAILED
        raise RuntimeError("Unknown GitHub Actions run status")


class CodePipelineProvider:
    def __init__(self, clients: dict[str, object] | None = None) -> None:
        self.clients = clients if clients is not None else {}

    def _client(self, region: str) -> object:
        if region not in self.clients:
            import boto3

            self.clients[region] = boto3.Session(region_name=region).client("codepipeline")
        return self.clients[region]

    def start(self, target: AWSCodePipelineTarget, task_id: UUID) -> str:
        response = self._client(target.region).start_pipeline_execution(
            name=target.name,
            clientRequestToken=str(task_id),
        )
        return response["pipelineExecutionId"]

    def status(self, target: AWSCodePipelineTarget, execution_id: str) -> RunState:
        response = self._client(target.region).get_pipeline_execution(
            pipelineName=target.name,
            pipelineExecutionId=execution_id,
        )
        status = response["pipelineExecution"]["status"]
        if status == "Succeeded":
            return RunState.SUCCEEDED
        if status in {"Failed", "Stopped", "Cancelled", "Superseded"}:
            return RunState.FAILED
        if status in {"InProgress", "Stopping"}:
            return RunState.RUNNING
        raise RuntimeError(f"Unknown CodePipeline status: {status}")


class DevOpsAgent:
    def __init__(
        self,
        github: GitHubActionsProvider | None = None,
        codepipeline: CodePipelineProvider | None = None,
        *,
        poll_interval: float = 10,
        timeout_seconds: float = 900,
    ) -> None:
        if poll_interval <= 0 or timeout_seconds <= 0:
            raise ValueError("poll_interval and timeout_seconds must be positive")
        self.github = github
        self.codepipeline = codepipeline
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds

    def execute(self, task: WorkflowTask) -> str:
        if task.agent != AgentKind.DEVOPS or task.devops_target is None:
            raise ValueError("DevOps task requires a CI/CD target")
        target = task.devops_target
        if isinstance(target, GitHubActionsTarget):
            provider = self.github
        else:
            provider = self.codepipeline
        if provider is None:
            raise RuntimeError(f"Provider is not configured: {target.provider}")
        execution_id = provider.start(target, task.id)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            state = provider.status(target, execution_id)
            if state == RunState.SUCCEEDED:
                return json.dumps(
                    {"provider": target.provider, "execution_id": execution_id, "status": state}
                )
            if state == RunState.FAILED:
                raise RuntimeError(
                    json.dumps(
                        {"provider": target.provider, "execution_id": execution_id, "status": state}
                    )
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    json.dumps(
                        {"provider": target.provider, "execution_id": execution_id, "status": "UNKNOWN"}
                    )
                )
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
