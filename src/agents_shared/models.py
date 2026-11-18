from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from agents_shared.results import MAX_RESULT_LENGTH


class AgentKind(StrEnum):
    CODE = "CODE"
    SECURITY = "SECURITY"
    DEVOPS = "DEVOPS"
    INFRA = "INFRA"
    MONITORING = "MONITORING"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GitHubActionsTarget(BaseModel):
    provider: Literal["github_actions"] = "github_actions"
    owner: str = Field(pattern=r"^[A-Za-z0-9-]+$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    ref: str = Field(min_length=1)
    inputs: dict[str, str] = Field(default_factory=dict)


class GitHubSourceTarget(BaseModel):
    provider: Literal["github"] = "github"
    owner: str = Field(pattern=r"^[A-Za-z0-9-]+$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    ref: str = Field(min_length=1, max_length=255)


class AWSCodePipelineTarget(BaseModel):
    provider: Literal["aws_codepipeline"] = "aws_codepipeline"
    name: str = Field(pattern=r"^[A-Za-z0-9.@_-]{1,100}$")
    region: str = Field(min_length=1)


DevOpsTarget = Annotated[
    GitHubActionsTarget | AWSCodePipelineTarget, Field(discriminator="provider")
]


class CloudFormationTarget(BaseModel):
    provider: Literal["aws_cloudformation"] = "aws_cloudformation"
    stack_name: str = Field(min_length=1)
    region: str = Field(min_length=1)
    action: Literal["inspect", "deploy"] = "inspect"
    template_url: str | None = None
    capabilities: list[Literal["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_template_for_deploy(self) -> "CloudFormationTarget":
        if self.action == "deploy" and not (self.template_url and self.template_url.startswith("https://")):
            raise ValueError("deploy requires an HTTPS template_url")
        return self


class CloudWatchMetricTarget(BaseModel):
    provider: Literal["aws_cloudwatch"] = "aws_cloudwatch"
    region: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    metric_name: str = Field(min_length=1)
    dimensions: dict[str, str] = Field(default_factory=dict)
    statistic: Literal["Average", "Sum", "Minimum", "Maximum", "SampleCount"] = "Average"
    comparison: Literal["lte", "gte"]
    threshold: float
    period_seconds: int = Field(default=60, ge=60)
    lookback_minutes: int = Field(default=5, ge=1, le=60)

    @model_validator(mode="after")
    def check_period(self) -> "CloudWatchMetricTarget":
        if self.period_seconds % 60:
            raise ValueError("period_seconds must be a multiple of 60")
        return self


class WorkflowRequest(BaseModel):
    request: str = Field(min_length=1, max_length=4000)
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    source_target: GitHubSourceTarget | None = None
    devops_target: DevOpsTarget | None = None
    infrastructure_target: CloudFormationTarget | None = None
    monitoring_target: CloudWatchMetricTarget | None = None
    code_change: bool = False

    @model_validator(mode="after")
    def validate_targets(self) -> "WorkflowRequest":
        if self.code_change:
            if self.source_target is None:
                raise ValueError("code_change requires source_target")
            if any((self.devops_target, self.infrastructure_target, self.monitoring_target)):
                raise ValueError("code proposals must be reviewed before deployment")
        elif not any((self.source_target, self.devops_target, self.infrastructure_target, self.monitoring_target)):
            raise ValueError("at least one target is required")
        return self


class WorkflowTask(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    agent: AgentKind
    instruction: str
    status: TaskStatus = TaskStatus.PENDING
    output: str | None = None
    source_target: GitHubSourceTarget | None = None
    devops_target: DevOpsTarget | None = None
    infrastructure_target: CloudFormationTarget | None = None
    monitoring_target: CloudWatchMetricTarget | None = None


class WorkflowPlan(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    request: WorkflowRequest
    tasks: list[WorkflowTask]


class WorkflowTaskMessage(BaseModel):
    workflow_id: UUID
    task: WorkflowTask


class TaskResultMessage(BaseModel):
    workflow_id: UUID
    task_id: UUID
    status: TaskStatus
    output: str = Field(max_length=MAX_RESULT_LENGTH)
