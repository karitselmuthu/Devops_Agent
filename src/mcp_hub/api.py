"""Read-only integration tools for CI/CD and AWS observability."""

import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from agents.devops_agent import CodePipelineProvider, GitHubActionsProvider
from agents.infrastructure_agent import InfrastructureAgent
from agents.monitoring_agent import MonitoringAgent
from agents_shared.models import (
    AWSCodePipelineTarget,
    AgentKind,
    CloudFormationTarget,
    CloudWatchMetricTarget,
    GitHubActionsTarget,
    WorkflowTask,
)


router = APIRouter(tags=["integrations"])


class ToolDescription(BaseModel):
    name: str
    description: str


TOOLS = [
    ToolDescription(name="github_actions.run_status", description="Read a GitHub Actions run status"),
    ToolDescription(name="aws_codepipeline.execution_status", description="Read a CodePipeline execution status"),
    ToolDescription(name="aws_cloudformation.stack_status", description="Inspect a CloudFormation stack"),
    ToolDescription(name="aws_cloudwatch.metric_check", description="Evaluate a CloudWatch metric threshold"),
]


class ToolCall(BaseModel):
    target: dict[str, Any]
    execution_id: str | None = None


def require_allowed(variable: str, value: str) -> None:
    if os.environ.get("AGENTS_MODE") != "aws":
        return
    allowed = {item.strip() for item in os.environ.get(variable, "").split(",") if item.strip()}
    if value not in allowed:
        raise HTTPException(status_code=403, detail="Target is not allowed")


@router.get("/tools", response_model=list[ToolDescription])
def list_tools() -> list[ToolDescription]:
    return TOOLS


@router.post("/tools/{tool_name}/invoke")
def invoke_tool(tool_name: str, call: ToolCall) -> dict[str, Any]:
    try:
        if tool_name == "github_actions.run_status":
            target = GitHubActionsTarget.model_validate(call.target)
            require_allowed("AGENTS_ALLOWED_REPOS", f"{target.owner}/{target.repo}")
            if not call.execution_id:
                raise ValueError("execution_id is required")
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise HTTPException(status_code=503, detail="GitHub connector is not configured")
            return {"status": GitHubActionsProvider(token).status(target, call.execution_id)}
        if tool_name == "aws_codepipeline.execution_status":
            target = AWSCodePipelineTarget.model_validate(call.target)
            require_allowed("AGENTS_ALLOWED_PIPELINES", target.name)
            if not call.execution_id:
                raise ValueError("execution_id is required")
            return {"status": CodePipelineProvider().status(target, call.execution_id)}
        if tool_name == "aws_cloudformation.stack_status":
            target = CloudFormationTarget.model_validate(call.target)
            require_allowed("AGENTS_ALLOWED_STACKS", target.stack_name)
            if target.action != "inspect":
                raise ValueError("This tool only inspects stacks")
            task = WorkflowTask(agent=AgentKind.INFRA, instruction="inspect", infrastructure_target=target)
            return json.loads(InfrastructureAgent().execute(task))
        if tool_name == "aws_cloudwatch.metric_check":
            target = CloudWatchMetricTarget.model_validate(call.target)
            task = WorkflowTask(agent=AgentKind.MONITORING, instruction="check", monitoring_target=target)
            return json.loads(MonitoringAgent().execute(task))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Tool not found")
