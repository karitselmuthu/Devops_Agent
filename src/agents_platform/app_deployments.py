"""Repository deployment plans and lifecycle contracts."""

import subprocess
import re
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


class DeploymentKind(StrEnum):
    CONTAINER = "container"
    SAM_LAMBDA = "sam_lambda"


ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
RESERVED_ENV_PREFIXES = ("AWS_", "ECS_")
MAX_CONFIG_ENTRIES = 30


class AppPlanRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)
    ref: str = Field(default="main", min_length=1, max_length=255)
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{2,30}$")
    environment: Literal["staging", "production"] = "staging"
    port: int | None = Field(default=None, ge=1, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$", max_length=256)
    # Plain (non-secret) settings; visible in the plan and the task definition. Never put secrets here.
    environment_variables: dict[str, str] = Field(default_factory=dict)
    # NAME -> Secrets Manager secret name under agents-app/<app name>/; the platform never sees the value.
    secret_refs: dict[str, str] = Field(default_factory=dict)
    # Relative to the repository root; the image is always built with the repository root as context.
    dockerfile_path: str = Field(default="Dockerfile", pattern=r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$", max_length=256)

    @model_validator(mode="after")
    def valid_dockerfile_path(self) -> "AppPlanRequest":
        if ".." in self.dockerfile_path.split("/"):
            raise ValueError("dockerfile_path cannot contain '..'")
        return self

    @model_validator(mode="after")
    def valid_repository(self) -> "AppPlanRequest":
        source = self.source_target()
        if self.name is None:
            name = re.sub(r"[^a-z0-9]+", "-", source.repo.lower()).strip("-")[:31].rstrip("-")
            if not name or not name[0].isalpha():
                name = f"app-{name}"[:31].rstrip("-")
            if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", name):
                raise ValueError("Repository name cannot become a valid app name; provide one explicitly")
            self.name = name
        return self

    @model_validator(mode="after")
    def valid_configuration(self) -> "AppPlanRequest":
        if len(self.environment_variables) + len(self.secret_refs) > MAX_CONFIG_ENTRIES:
            raise ValueError(f"At most {MAX_CONFIG_ENTRIES} environment variables and secret references are allowed")
        for name, value in self.environment_variables.items():
            if not ENV_NAME.fullmatch(name) or name.startswith(RESERVED_ENV_PREFIXES):
                raise ValueError(f"Invalid environment variable name: {name!r}")
            if len(value) > 512 or "\n" in value or "\r" in value:
                raise ValueError(f"Invalid value for {name}: at most 512 characters, one line")
        for name, secret in self.secret_refs.items():
            if not ENV_NAME.fullmatch(name) or name.startswith(RESERVED_ENV_PREFIXES):
                raise ValueError(f"Invalid secret variable name: {name!r}")
            if not re.fullmatch(rf"agents-app/{re.escape(self.name)}/[A-Za-z0-9_.-]{{1,128}}", secret) or ".." in secret:
                raise ValueError(f"Secret {name} must be named agents-app/{self.name}/<KEY>")
        if set(self.environment_variables) & set(self.secret_refs):
            raise ValueError("A variable cannot be both a plain value and a secret")
        return self

    def source_target(self) -> GitHubSourceTarget:
        return RepositoryDraft(
            repository_url=self.repository_url,
            ref=self.ref,
            service=self.name or "app",
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
    deployment_kind: DeploymentKind = DeploymentKind.CONTAINER
    template_digest: str | None = None
    prerequisites: list[str] = Field(default_factory=list)
    access_mode: Literal["public_https", "aws_iam"] = "public_https"
    build_id: str | None = None
    service_url: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _missing_message(repository: Path, dockerfile_path: str) -> str:
    found = sorted(
        path.relative_to(repository).as_posix() for path in repository.rglob("Dockerfile")
        if ".git" not in path.parts and not path.is_symlink()
    )[:5]
    message = f"A root Dockerfile (or the Dockerfile path you gave: {dockerfile_path}) or supported SAM template.yaml is required"
    if found:
        message += f". Dockerfiles found in this repository: {', '.join(found)}; enter one as the Dockerfile path"
    return message


def create_app_plan(request: AppPlanRequest, created_by: str) -> AppDeployment:
    from agents_platform.sam_blueprint import load_sam_template, sam_resources, sam_template_digest

    source = request.source_target()
    with TemporaryDirectory() as temporary:
        repository = Path(temporary) / "source"
        checkout_source(source, repository)
        dockerfile = repository / request.dockerfile_path
        sam_file = repository / "template.yaml"
        if sam_file.is_file():
            load_sam_template(sam_file)
            kind = DeploymentKind.SAM_LAMBDA
            digest = sam_template_digest(sam_file)
        elif dockerfile.is_file() and not dockerfile.is_symlink() and dockerfile.resolve().is_relative_to(repository.resolve()):
            docker_text = dockerfile.read_text(errors="replace")
            if request.port is None:
                exposed = re.findall(r"(?im)^\s*EXPOSE\s+(\d+)(?:/tcp)?\s*(?:#.*)?$", docker_text)
                if len(exposed) != 1:
                    raise ValueError("Container HTTP port could not be inferred; provide one explicitly")
                request.port = int(exposed[0])
            if request.health_path == "/health":
                health = re.search(
                    rf"https?://(?:127\.0\.0\.1|localhost):{request.port}(/[A-Za-z0-9/_-]+)",
                    docker_text,
                )
                if health:
                    request.health_path = health.group(1)
            kind = DeploymentKind.CONTAINER
            digest = None
        else:
            raise ValueError(_missing_message(repository, request.dockerfile_path))
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository,
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    deployment = AppDeployment(
        request=request,
        source_commit=commit,
        created_by=created_by,
        resources=sam_resources() if kind == DeploymentKind.SAM_LAMBDA else [
            PlannedResource(service="ECR", name="Application image repository", purpose="Store built images"),
            PlannedResource(service="CodeBuild", name="Container build", purpose="Build and push one image", billed_while_running=True),
            PlannedResource(service="ECS Fargate", name="Web service", purpose="Run the container in the shared cluster", billed_while_running=True),
            PlannedResource(service="Application Load Balancer", name="Routing rule", purpose="Route this app through the platform load balancer"),
            PlannedResource(service="CloudFront", name="Application HTTPS URL", purpose="Give the app a separate HTTPS address; usage billed"),
        ],
        ecr_repository="pending",
        stack_name="pending",
        image_tag=commit[:12],
        deployment_kind=kind,
        template_digest=digest,
        prerequisites=([
            "Bedrock model access must be enabled in the target region before inference.",
            "Private retrieval needs a populated S3 Vectors bucket and index; this SAM stack does not create them.",
            "The Lambda Function URL requires AWS SigV4 authentication.",
        ] if kind == DeploymentKind.SAM_LAMBDA else []),
        access_mode="aws_iam" if kind == DeploymentKind.SAM_LAMBDA else "public_https",
    )
    if kind == DeploymentKind.SAM_LAMBDA and (request.environment_variables or request.secret_refs):
        raise ValueError("Environment variables and secret references are supported for container apps only; "
                         "a SAM app defines its own configuration in template.yaml")
    if request.secret_refs:
        deployment.resources.append(PlannedResource(
            service="Secrets Manager", name=f"{len(request.secret_refs)} secret reference(s), read-only",
            purpose="Inject secrets into the container at start; the platform never stores the values",
        ))
        deployment.prerequisites += [
            f"Create secret {secret} in Secrets Manager (us-east-1) before approval; it is injected as {name}."
            for name, secret in request.secret_refs.items()
        ]
    suffix = deployment.id.hex[:12]
    deployment.ecr_repository = f"agents-app-{request.environment}-{suffix}"
    deployment.stack_name = f"AgentsApp-{request.environment}-{suffix}"
    if kind == DeploymentKind.CONTAINER:
        deployment.resources[0].name = deployment.ecr_repository
        deployment.resources[1].name = "Shared platform CodeBuild project"
        deployment.resources[2].name = f"{request.name[:30]}-{deployment.id.hex[:8]}"
        deployment.resources[3].name = f"{request.name[:30]}-{deployment.id.hex[:8]}"
        deployment.resources[4].name = f"{request.name[:30]}-{deployment.id.hex[:8]}"
    else:
        deployment.ecr_repository = ""
    return deployment
