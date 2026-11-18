from uuid import UUID
import os
from contextlib import asynccontextmanager
from threading import Event, Thread

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agents_platform.auth import CognitoVerifier, Principal
from agents_platform.config import database_url
from agents_platform.orchestrator import Orchestrator, WorkflowNotFound
from agents_platform.local_runtime import run_local_forever
from agents_platform.persistence import DurableWorkflowStore
from agents_platform.app_deployments import AppDeployment, AppPlanRequest, create_app_plan
from agents_platform.app_store import AppNotFound, DurableAppStore, InMemoryAppStore, InvalidAppTransition
from agents_platform.portal import protected_router as protected_portal_router
from agents_platform.portal import public_router as public_portal_router
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import WorkflowPlan, WorkflowRequest
from mcp_hub.api import router as integration_router


def create_app(
    orchestrator: Orchestrator | DurableWorkflowStore | None = None,
    verifier: CognitoVerifier | None = None,
    app_store: InMemoryAppStore | DurableAppStore | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        local = os.environ.get("AGENTS_MODE") != "aws" and isinstance(application.state.orchestrator, Orchestrator)
        if not local:
            yield
            return
        stop = Event()
        thread = Thread(target=run_local_forever, args=(application.state.orchestrator, stop), daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    app = FastAPI(title="Agents Platform", version="0.1.0", lifespan=lifespan)
    if orchestrator is not None:
        app.state.orchestrator = orchestrator
    elif os.environ.get("AGENTS_MODE") == "aws":
        app.state.orchestrator = DurableWorkflowStore(database_url())
    else:
        app.state.orchestrator = Orchestrator(InMemoryTaskBroker())
    app.state.app_store = app_store or (
        DurableAppStore(database_url()) if os.environ.get("AGENTS_MODE") == "aws" else InMemoryAppStore()
    )
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
    app.include_router(public_portal_router)
    app.include_router(protected_portal_router, dependencies=[Depends(reader)])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/portal/config")
    def portal_config() -> dict[str, str | bool]:
        aws_mode = os.environ.get("AGENTS_MODE") == "aws"
        return {
            "authentication_required": aws_mode,
            "cognito_domain": os.environ.get("COGNITO_DOMAIN", "") if aws_mode else "",
            "cognito_client_id": os.environ.get("COGNITO_APP_CLIENT_ID", "") if aws_mode else "",
            "app_deployment_enabled": aws_mode,
        }

    @app.post("/apps/plans", response_model=AppDeployment, status_code=status.HTTP_201_CREATED)
    def plan_app(request: AppPlanRequest, user: Principal = Depends(operator)) -> AppDeployment:
        if os.environ.get("AGENTS_MODE") == "aws":
            source = request.source_target()
            allowed = {item.strip() for item in os.environ.get("AGENTS_ALLOWED_REPOS", "").split(",") if item.strip()}
            if f"{source.owner}/{source.repo}" not in allowed:
                raise HTTPException(status_code=403, detail="Repository is not allowed")
            if request.environment != os.environ.get("AGENTS_ENVIRONMENT"):
                raise HTTPException(status_code=422, detail="Environment does not match this platform")
        try:
            plan = create_app_plan(request, user.subject)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return app.state.app_store.create(plan)

    @app.get("/apps", response_model=list[AppDeployment])
    def list_apps(user: Principal = Depends(reader)) -> list[AppDeployment]:
        return app.state.app_store.list_deployments()

    @app.get("/apps/{deployment_id}", response_model=AppDeployment)
    def get_app(deployment_id: UUID, user: Principal = Depends(reader)) -> AppDeployment:
        try:
            return app.state.app_store.get(deployment_id)
        except AppNotFound as exc:
            raise HTTPException(status_code=404, detail="Application plan not found") from exc

    @app.post("/apps/{deployment_id}/approve", response_model=AppDeployment)
    def approve_app(deployment_id: UUID, user: Principal = Depends(principal)) -> AppDeployment:
        if "agents-admins" not in user.groups:
            raise HTTPException(status_code=403, detail="Admin role required")
        if os.environ.get("AGENTS_MODE") != "aws":
            raise HTTPException(status_code=503, detail="AWS deployment is available only in AWS mode")
        try:
            return app.state.app_store.approve(deployment_id)
        except AppNotFound as exc:
            raise HTTPException(status_code=404, detail="Application plan not found") from exc
        except InvalidAppTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/apps/{deployment_id}", response_model=AppDeployment)
    def delete_app(deployment_id: UUID, user: Principal = Depends(principal)) -> AppDeployment:
        if "agents-admins" not in user.groups:
            raise HTTPException(status_code=403, detail="Admin role required")
        try:
            plan = app.state.app_store.get(deployment_id)
            if os.environ.get("AGENTS_MODE") != "aws" and plan.status != "PLANNED":
                raise HTTPException(status_code=503, detail="AWS cleanup is available only in AWS mode")
            return app.state.app_store.request_delete(deployment_id)
        except AppNotFound as exc:
            raise HTTPException(status_code=404, detail="Application plan not found") from exc
        except InvalidAppTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
