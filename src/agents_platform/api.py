from uuid import UUID
import os

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agents_platform.auth import CognitoVerifier, Principal
from agents_platform.config import database_url
from agents_platform.orchestrator import Orchestrator, WorkflowNotFound
from agents_platform.persistence import DurableWorkflowStore
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import WorkflowPlan, WorkflowRequest
from mcp_hub.api import router as integration_router


def create_app(
    orchestrator: Orchestrator | DurableWorkflowStore | None = None,
    verifier: CognitoVerifier | None = None,
) -> FastAPI:
    app = FastAPI(title="Agents Platform", version="0.1.0")
    if orchestrator is not None:
        app.state.orchestrator = orchestrator
    elif os.environ.get("AGENTS_MODE") == "aws":
        app.state.orchestrator = DurableWorkflowStore(database_url())
    else:
        app.state.orchestrator = Orchestrator(InMemoryTaskBroker())
    if os.environ.get("AGENTS_MODE") == "aws" and verifier is None:
        verifier = CognitoVerifier(
            os.environ["AWS_REGION"],
            os.environ["COGNITO_USER_POOL_ID"],
            os.environ["COGNITO_APP_CLIENT_ID"],
        )
    bearer = HTTPBearer(auto_error=False)

    def principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if verifier is None:
            return Principal("local", frozenset({"agents-operators", "agents-admins"}))
        if credentials is None:
            raise HTTPException(status_code=401, detail="Access token required")
        return verifier.verify(credentials.credentials)

    def reader(user: Principal = Depends(principal)) -> Principal:
        if not user.groups.intersection({"agents-readers", "agents-operators", "agents-admins"}):
            raise HTTPException(status_code=403, detail="Reader role required")
        return user

    def operator(user: Principal = Depends(principal)) -> Principal:
        if not user.groups.intersection({"agents-operators", "agents-admins"}):
            raise HTTPException(status_code=403, detail="Operator role required")
        return user
    app.include_router(integration_router, prefix="/integrations", dependencies=[Depends(reader)])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/workflows", response_model=WorkflowPlan, status_code=status.HTTP_201_CREATED)
    def create_workflow(request: WorkflowRequest, user: Principal = Depends(operator)) -> WorkflowPlan:
        if os.environ.get("AGENTS_MODE") == "aws":
            source = request.source_target
            devops = request.devops_target
            infra = request.infrastructure_target
            checks = [
                ("AGENTS_ALLOWED_REPOS", f"{source.owner}/{source.repo}" if source else None),
                (
                    "AGENTS_ALLOWED_REPOS",
                    f"{devops.owner}/{devops.repo}" if getattr(devops, "provider", None) == "github_actions" else None,
                ),
                (
                    "AGENTS_ALLOWED_PIPELINES",
                    devops.name if getattr(devops, "provider", None) == "aws_codepipeline" else None,
                ),
                ("AGENTS_ALLOWED_STACKS", infra.stack_name if infra else None),
            ]
            for variable, target in checks:
                allowed = {item.strip() for item in os.environ.get(variable, "").split(",") if item.strip()}
                if target is not None and target not in allowed:
                    raise HTTPException(status_code=403, detail="Target is not allowed")
        if (
            request.infrastructure_target is not None
            and request.infrastructure_target.action == "deploy"
            and "agents-admins" not in user.groups
        ):
            raise HTTPException(status_code=403, detail="Admin role required for infrastructure changes")
        return app.state.orchestrator.create(request)

    @app.get("/workflows/{workflow_id}", response_model=WorkflowPlan)
    def get_workflow(workflow_id: UUID, user: Principal = Depends(reader)) -> WorkflowPlan:
        try:
            return app.state.orchestrator.get(workflow_id)
        except WorkflowNotFound as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc

    return app


app = create_app()
