from fastapi.testclient import TestClient

from agents_platform.api import create_app
from agents_platform.auth import Principal


class FakeVerifier:
    def verify(self, token: str) -> Principal:
        groups = {
            "reader": {"agents-readers"},
            "operator": {"agents-operators"},
            "admin": {"agents-admins"},
        }
        return Principal(token, frozenset(groups.get(token, set())))


BODY = {
    "request": "deploy", "service": "api", "environment": "staging",
    "devops_target": {"provider": "aws_codepipeline", "name": "api", "region": "us-east-1"},
}


def test_api_requires_roles_and_admin_for_infrastructure_deploy() -> None:
    client = TestClient(create_app(verifier=FakeVerifier()))
    assert client.post("/workflows", json=BODY).status_code == 401
    reader = {"Authorization": "Bearer reader"}
    operator = {"Authorization": "Bearer operator"}
    admin = {"Authorization": "Bearer admin"}
    assert client.post("/workflows", json=BODY, headers=reader).status_code == 403
    response = client.post("/workflows", json=BODY, headers=operator)
    assert response.status_code == 201
    assert client.get(f"/workflows/{response.json()['id']}", headers=reader).status_code == 200

    deploy = BODY | {
        "infrastructure_target": {
            "provider": "aws_cloudformation",
            "stack_name": "api",
            "region": "us-east-1",
            "action": "deploy",
            "template_url": "https://example.com/template.yaml",
        }
    }
    assert client.post("/workflows", json=deploy, headers=operator).status_code == 403
    assert client.post("/workflows", json=deploy, headers=admin).status_code == 201
