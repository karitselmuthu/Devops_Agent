"""Guided repository intake for the web portal."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from agents_shared.models import GitHubSourceTarget, WorkflowPlan, WorkflowRequest
from planning_agent.planner import create_plan


_OWNER = re.compile(r"^[A-Za-z0-9-]+$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+$")
_ASSETS = Path(__file__).parent / "portal_assets"


class RepositoryDraft(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)
    ref: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_repository(self) -> "RepositoryDraft":
        self.source_target()
        return self

    def source_target(self) -> GitHubSourceTarget:
        url = urlsplit(self.repository_url)
        path = url.path.removesuffix("/").split("/")
        if (
            url.scheme != "https"
            or url.netloc.lower() != "github.com"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or len(path) != 3
            or path[0] != ""
        ):
            raise ValueError("Enter a GitHub repository URL such as https://github.com/owner/repo")
        owner, repo = path[1], path[2].removesuffix(".git")
        if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) or repo in {".", ".."}:
            raise ValueError("Enter a GitHub repository URL such as https://github.com/owner/repo")
        return GitHubSourceTarget(owner=owner, repo=repo, ref=self.ref)

    def workflow_request(self) -> WorkflowRequest:
        return WorkflowRequest(
            request=f"Scan {self.service} source before deployment",
            service=self.service,
            environment=self.environment,
            source_target=self.source_target(),
        )


class RepositoryPreview(BaseModel):
    plan: WorkflowPlan
    deployment_ready: bool = False
    next_step: str = "Connect a build pipeline and review an AWS resource plan before deployment."


public_router = APIRouter()
protected_router = APIRouter()


@public_router.get("/", include_in_schema=False)
def portal_home() -> FileResponse:
    return FileResponse(
        _ASSETS / "index.html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self' https://*.amazoncognito.com; "
                "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_router.get("/portal/style.css", include_in_schema=False)
def portal_style() -> FileResponse:
    return FileResponse(_ASSETS / "style.css", media_type="text/css")


@public_router.get("/portal/app.js", include_in_schema=False)
def portal_script() -> FileResponse:
    return FileResponse(_ASSETS / "app.js", media_type="application/javascript")


@protected_router.post("/portal/preview", response_model=RepositoryPreview)
def preview_repository(draft: RepositoryDraft) -> RepositoryPreview:
    return RepositoryPreview(plan=create_plan(draft.workflow_request()))
