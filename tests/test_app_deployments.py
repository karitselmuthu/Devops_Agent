import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

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


def test_repository_url_can_infer_app_name_and_container_settings(monkeypatch) -> None:
    def checkout(_target, destination: Path) -> None:
        destination.mkdir()
        (destination / "Dockerfile").write_text(
            'FROM python:3.12-slim\nEXPOSE 8000\n'
            'HEALTHCHECK CMD python -c "http://127.0.0.1:8000/health/ready"\n'
        )

    monkeypatch.setattr(app_deployments, "checkout_source", checkout)
    monkeypatch.setattr(
        app_deployments.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "a" * 40 + "\n", ""),
    )
    plan = create_app_plan(
        AppPlanRequest(repository_url="https://github.com/acme/Enterprise_AI_Model_Router"),
        "operator-1",
    )
    assert plan.request.name == "enterprise-ai-model-router"
    assert plan.request.ref == "main"
    assert plan.request.port == 8000
    assert plan.request.health_path == "/health/ready"


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


def _fake_checkout(monkeypatch, files: dict[str, str]) -> None:
    def checkout(_target, destination: Path) -> None:
        destination.mkdir()
        for name, text in files.items():
            (destination / name).parent.mkdir(parents=True, exist_ok=True)
            (destination / name).write_text(text)

    monkeypatch.setattr(app_deployments, "checkout_source", checkout)
    monkeypatch.setattr(
        app_deployments.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "a" * 40 + "\n", ""),
    )


def test_plan_can_use_a_dockerfile_in_a_subdirectory(monkeypatch) -> None:
    _fake_checkout(monkeypatch, {
        "simulation/live_stack/Dockerfile":
            'FROM python:3.12-slim\nEXPOSE 8765\nHEALTHCHECK CMD python -c "http://127.0.0.1:8765/ready"\n',
    })
    plan = create_app_plan(AppPlanRequest(
        repository_url="https://github.com/acme/web-app",
        dockerfile_path="simulation/live_stack/Dockerfile",
    ), "operator-1")
    assert plan.request.dockerfile_path == "simulation/live_stack/Dockerfile"
    assert plan.request.port == 8765
    assert plan.request.health_path == "/ready"


def test_missing_dockerfile_names_other_dockerfiles_it_found(monkeypatch) -> None:
    _fake_checkout(monkeypatch, {"simulation/live_stack/Dockerfile": "FROM scratch\n"})
    with pytest.raises(ValueError, match="simulation/live_stack/Dockerfile"):
        create_app_plan(AppPlanRequest(repository_url="https://github.com/acme/web-app"), "operator-1")


@pytest.mark.parametrize("bad", ["../Dockerfile", "/etc/passwd", "a/../../Dockerfile", "-f/x", "a b/Dockerfile", ".."])
def test_dockerfile_path_cannot_escape_the_repository(bad: str) -> None:
    with pytest.raises(ValidationError):
        AppPlanRequest(repository_url="https://github.com/acme/web-app", dockerfile_path=bad)


def test_dockerfile_symlink_outside_repository_is_rejected(monkeypatch, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("EXPOSE 22\n")

    def checkout(_target, destination: Path) -> None:
        destination.mkdir()
        (destination / "Dockerfile").symlink_to(secret)

    monkeypatch.setattr(app_deployments, "checkout_source", checkout)
    with pytest.raises(ValueError, match="Dockerfile"):
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


@pytest.mark.parametrize("durable", [False, True])
def test_recent_application_plans_are_newest_first(tmp_path: Path, durable: bool) -> None:
    if durable:
        store = DurableAppStore(f"sqlite:///{tmp_path / 'apps.db'}")
        Base.metadata.create_all(store.engine)
    else:
        store = InMemoryAppStore()
    old = app_deployments.AppDeployment(
        id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"), request=request(),
        source_commit="a" * 40, created_by="operator-1", resources=[],
        ecr_repository="old", stack_name="old", image_tag="a" * 12,
        created_at=datetime(2026, 11, 19, tzinfo=timezone.utc),
    )
    new = old.model_copy(update={
        "id": UUID("00000000-0000-4000-8000-000000000001"),
        "ecr_repository": "new", "stack_name": "new",
        "created_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
    })
    store.create(old)
    store.create(new)

    assert [plan.id for plan in store.list_deployments()] == [new.id, old.id]


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
