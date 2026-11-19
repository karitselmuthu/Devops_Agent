"""Fixed GitHub-to-container deployment plan and lifecycle contracts."""

import subprocess
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from agents_platform.portal import RepositoryDraft
from agents_platform.source import checkout_source
from agents_shared.models import GitHubSourceTarget


class DeploymentStatus(StrEnum):
    PLANNED = "PLANNED"
    APPROVED = "APPROVED"
    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class AppPlanRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)
    ref: str = Field(min_length=1, max_length=255)
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,30}$")
    environment: Literal["staging", "production"] = "staging"
    port: int = Field(ge=1, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$", max_length=256)

    @model_validator(mode="after")
    def valid_repository(self) -> "AppPlanRequest":
        self.source_target()
        return self

    def source_target(self) -> GitHubSourceTarget:
        return RepositoryDraft(
            repository_url=self.repository_url,
            ref=self.ref,
            service=self.name,
            environment=self.environment,
        ).source_target()


class PlannedResource(BaseModel):
    service: str
    name: str
    purpose: str
    billed_while_running: bool = False


class AppDeployment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    request: AppPlanRequest
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    created_by: str
    status: DeploymentStatus = DeploymentStatus.PLANNED
    resources: list[PlannedResource]
    ecr_repository: str
    stack_name: str
    image_tag: str
    build_id: str | None = None
    service_url: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def create_app_plan(request: AppPlanRequest, created_by: str) -> AppDeployment:
    source = request.source_target()
    with TemporaryDirectory() as temporary:
        repository = Path(temporary) / "source"
        checkout_source(source, repository)
        dockerfile = repository / "Dockerfile"
        if not dockerfile.is_file() or dockerfile.is_symlink():
            raise ValueError("A root Dockerfile is required for this deployment blueprint")
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    deployment = AppDeployment(
        request=request,
        source_commit=commit,
        created_by=created_by,
        resources=[
            PlannedResource(service="ECR", name="Application image repository", purpose="Store built images"),
            PlannedResource(service="CodeBuild", name="Container build", purpose="Build and push one image", billed_while_running=True),
            PlannedResource(service="ECS Fargate", name="Web service", purpose="Run the container in the shared cluster", billed_while_running=True),
            PlannedResource(service="Application Load Balancer", name="Routing rule", purpose="Route this app through the platform load balancer"),
            PlannedResource(service="CloudFront", name="Application HTTPS URL", purpose="Give the app a separate HTTPS address; usage billed"),
        ],
        ecr_repository="pending",
        stack_name="pending",
        image_tag=commit[:12],
    )
    suffix = deployment.id.hex[:12]
    deployment.ecr_repository = f"agents-app-{request.environment}-{suffix}"
    deployment.stack_name = f"AgentsApp-{request.environment}-{suffix}"
    deployment.resources[0].name = deployment.ecr_repository
    deployment.resources[1].name = "Shared platform CodeBuild project"
    deployment.resources[2].name = f"{request.name[:30]}-{deployment.id.hex[:8]}"
    deployment.resources[3].name = f"{request.name[:30]}-{deployment.id.hex[:8]}"
    deployment.resources[4].name = f"{request.name[:30]}-{deployment.id.hex[:8]}"
    return deployment
