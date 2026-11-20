from fastapi.testclient import TestClient
import time

from agents_platform.api import create_app
from agents_platform.auth import Principal
from agents_platform.local_runtime import run_local_once
from agents_platform.orchestrator import Orchestrator
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import WorkflowRequest


def test_portal_serves_page_and_previews_repository() -> None:
    client = TestClient(create_app())
    page = client.get("/")
    assert page.status_code == 200
    assert "Connect a repository" in page.text
    assert 'id="task-summary"' in page.text
    assert client.get("/portal/style.css").status_code == 200
    assert client.get("/portal/app.js").status_code == 200
    assert client.get("/portal/config").json()["authentication_required"] is False
    assert "script-src 'self'" in page.headers["content-security-policy"]

    response = client.post(
        "/portal/preview",
        json={
            "repository_url": "https://github.com/acme/billing.git",
            "ref": "main",
            "service": "billing",
            "environment": "staging",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_ready"] is False
    assert body["plan"]["request"]["source_target"] == {
        "provider": "github", "owner": "acme", "repo": "billing", "ref": "main"
    }
    assert [task["agent"] for task in body["plan"]["tasks"]] == ["SECURITY"]


def test_portal_rejects_non_repository_urls() -> None:
    client = TestClient(create_app())
    draft = {"ref": "main", "service": "billing", "environment": "staging"}
    for url in (
        "https://github.com/acme/billing/tree/main",
        "https://evil.example/acme/billing",
        "https://github.com/acme/billing?token=secret",
        "https://github.com/acme/..",
    ):
        assert client.post("/portal/preview", json=draft | {"repository_url": url}).status_code == 422


def test_portal_preview_requires_reader_in_protected_mode() -> None:
    class Verifier:
        def verify(self, token: str) -> Principal:
            return Principal(token, frozenset({"agents-readers"}))

    client = TestClient(create_app(verifier=Verifier()))
    draft = {
        "repository_url": "https://github.com/acme/billing",
        "ref": "main", "service": "billing", "environment": "staging",
    }
    assert client.get("/").status_code == 200
    assert client.post("/portal/preview", json=draft).status_code == 401
    assert client.post("/portal/preview", json=draft, headers={"Authorization": "Bearer reader"}).status_code == 200


def test_local_runner_completes_queued_source_check(monkeypatch) -> None:
    monkeypatch.setattr("agents_platform.local_runtime.make_agent", lambda agent: lambda task: "scan complete")
    orchestrator = Orchestrator(InMemoryTaskBroker())
    request = WorkflowRequest.model_validate({
        "request": "Scan source", "service": "billing", "environment": "staging",
        "source_target": {"provider": "github", "owner": "acme", "repo": "billing", "ref": "main"},
    })
    plan = orchestrator.create(request)
    assert run_local_once(orchestrator)
    assert orchestrator.get(plan.id).tasks[0].status == "COMPLETED"
    assert not run_local_once(orchestrator)


def test_local_api_runs_source_check_in_process(monkeypatch) -> None:
    monkeypatch.setattr("agents_platform.local_runtime.make_agent", lambda agent: lambda task: "scan complete")
    with TestClient(create_app()) as client:
        response = client.post("/workflows", json={
            "request": "Scan source", "service": "billing", "environment": "staging",
            "source_target": {"provider": "github", "owner": "acme", "repo": "billing", "ref": "main"},
        })
        assert response.status_code == 201
        workflow_id = response.json()["id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            task = client.get(f"/workflows/{workflow_id}").json()["tasks"][0]
            if task["status"] == "COMPLETED":
                assert task["output"] == "scan complete"
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Local worker did not complete the source check")
