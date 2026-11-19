import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents_platform import app_deployments
from agents_platform import api as api_module
from agents_platform.app_deployments import AppPlanRequest, DeploymentStatus, create_app_plan
from agents_platform.app_store import DurableAppStore, InMemoryAppStore
from agents_platform.auth import Principal
from agents_platform.orchestrator import Orchestrator
from agents_platform.persistence import Base
from agents_shared.messaging import InMemoryTaskBroker
from pydantic import ValidationError


def request() -> AppPlanRequest:
    return AppPlanRequest(
        repository_url="https://github.com/acme/web-app", ref="main",
        name="web-app", environment="staging", port=8080,
    )


def test_health_path_must_be_an_absolute_path() -> None:
    with pytest.raises(ValidationError):
        AppPlanRequest.model_validate({**request().model_dump(), "health_path": "health/ready"})


def test_plan_requires_root_dockerfile_and_pins_commit(monkeypatch) -> None:
    def checkout(_target, destination: Path) -> None:
        destination.mkdir()
        (destination / "Dockerfile").write_text("FROM python:3.12-slim\n")

    monkeypatch.setattr(app_deployments, "checkout_source", checkout)
    monkeypatch.setattr(
        app_deployments.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "a" * 40 + "\n", ""),
    )
    plan = create_app_plan(request(), "operator-1")
    assert plan.status == DeploymentStatus.PLANNED
    assert plan.source_commit == "a" * 40
    assert plan.image_tag == "a" * 12
    assert plan.ecr_repository.startswith("agents-app-staging-")
    assert [item.service for item in plan.resources] == [
        "ECR", "CodeBuild", "ECS Fargate", "Application Load Balancer", "CloudFront",
    ]

    def no_dockerfile(_target, destination: Path) -> None:
        destination.mkdir()

    monkeypatch.setattr(app_deployments, "checkout_source", no_dockerfile)
    with pytest.raises(ValueError, match="root Dockerfile"):
        create_app_plan(request(), "operator-1")


@pytest.mark.parametrize("durable", [False, True])
def test_approval_and_deletion_have_idempotent_outbox(tmp_path: Path, durable: bool) -> None:
    if durable:
        store = DurableAppStore(f"sqlite:///{tmp_path / 'apps.db'}")
        Base.metadata.create_all(store.engine)
    else:
        store = InMemoryAppStore()
    plan = app_deployments.AppDeployment(
        request=request(), source_commit="a" * 40, created_by="operator-1",
        resources=[], ecr_repository="agents-app-staging-test", stack_name="AgentsApp-staging-test", image_tag="a" * 12,
    )
    store.create(plan)
    assert store.approve(plan.id).status == DeploymentStatus.APPROVED
    assert store.approve(plan.id).status == DeploymentStatus.APPROVED
    jobs = store.pending_jobs()
    assert len(jobs) == 1 and jobs[0].action == "deploy"
    store.mark_sent(jobs[0].id)
    assert store.pending_jobs() == []
    assert store.request_delete(plan.id).status == DeploymentStatus.DELETING
    assert store.request_delete(plan.id).status == DeploymentStatus.DELETING
    jobs = store.pending_jobs()
    assert len(jobs) == 1 and jobs[0].action == "delete"


def test_app_api_previews_locally_and_requires_admin_for_aws_approval(monkeypatch) -> None:
    def fake_plan(input: AppPlanRequest, subject: str):
        return app_deployments.AppDeployment(
            request=input, source_commit="a" * 40, created_by=subject,
            resources=[], ecr_repository="agents-app-staging-test",
            stack_name="AgentsApp-staging-test", image_tag="a" * 12,
        )

    monkeypatch.setattr(api_module, "create_app_plan", fake_plan)
    body = request().model_dump()
    local = TestClient(api_module.create_app())
    response = local.post("/apps/plans", json=body)
    assert response.status_code == 201
    deployment_id = response.json()["id"]
    assert local.get(f"/apps/{deployment_id}").status_code == 200
    assert local.post(f"/apps/{deployment_id}/approve").status_code == 503
    assert local.delete(f"/apps/{deployment_id}").json()["status"] == "DELETED"

    class Verifier:
        def verify(self, token: str) -> Principal:
            groups = {"operator": {"agents-operators"}, "admin": {"agents-admins"}}
            return Principal(token, frozenset(groups[token]))

    monkeypatch.setenv("AGENTS_MODE", "aws")
    monkeypatch.setenv("AGENTS_ALLOWED_REPOS", "acme/web-app")
    monkeypatch.setenv("AGENTS_ENVIRONMENT", "staging")
    store = InMemoryAppStore()
    aws = TestClient(api_module.create_app(
        orchestrator=Orchestrator(InMemoryTaskBroker()), verifier=Verifier(), app_store=store,
    ))
    operator = {"Authorization": "Bearer operator"}
    admin = {"Authorization": "Bearer admin"}
    response = aws.post("/apps/plans", json=body, headers=operator)
    assert response.status_code == 201
    deployment_id = response.json()["id"]
    assert aws.post(f"/apps/{deployment_id}/approve", headers=operator).status_code == 403
    assert aws.post(f"/apps/{deployment_id}/approve", headers=admin).json()["status"] == "APPROVED"
    assert len(store.pending_jobs()) == 1
