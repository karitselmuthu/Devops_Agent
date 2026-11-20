"""Durable app deployment plans and a transactional job outbox."""

from datetime import datetime, timezone
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, ForeignKey, String, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from agents_platform.app_deployments import AppDeployment, DeploymentStatus
from agents_platform.persistence import Base


class AppJob(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    deployment_id: UUID
    action: Literal["deploy", "delete"]


class AppDeploymentRow(Base):
    __tablename__ = "app_deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class AppJobOutboxRow(Base):
    __tablename__ = "app_job_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(36), ForeignKey("app_deployments.id"), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppNotFound(Exception):
    pass


class InvalidAppTransition(Exception):
    pass


def _approve(plan: AppDeployment) -> AppJob | None:
    if plan.status == DeploymentStatus.APPROVED:
        return None
    if plan.status != DeploymentStatus.PLANNED:
        raise InvalidAppTransition("Only a planned application can be approved")
    plan.status = DeploymentStatus.APPROVED
    plan.updated_at = datetime.now(timezone.utc)
    return AppJob(deployment_id=plan.id, action="deploy")


def _delete(plan: AppDeployment) -> AppJob | None:
    if plan.status in {DeploymentStatus.DELETED, DeploymentStatus.DELETING}:
        return None
    if plan.status in {DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING}:
        raise InvalidAppTransition("Wait for the current deployment to finish before deleting")
    if plan.status == DeploymentStatus.PLANNED:
        plan.status = DeploymentStatus.DELETED
        plan.updated_at = datetime.now(timezone.utc)
        return None
    plan.status = DeploymentStatus.DELETING
    plan.updated_at = datetime.now(timezone.utc)
    return AppJob(deployment_id=plan.id, action="delete")


class InMemoryAppStore:
    def __init__(self) -> None:
        self._plans: dict[UUID, AppDeployment] = {}
        self._jobs: dict[UUID, AppJob] = {}
        self._sent: set[UUID] = set()
        self._lock = RLock()

    def create(self, plan: AppDeployment) -> AppDeployment:
        with self._lock:
            self._plans[plan.id] = plan.model_copy(deep=True)
            return plan.model_copy(deep=True)

    def get(self, deployment_id: UUID) -> AppDeployment:
        with self._lock:
            if deployment_id not in self._plans:
                raise AppNotFound
            return self._plans[deployment_id].model_copy(deep=True)

    def list_deployments(self, limit: int = 50) -> list[AppDeployment]:
        with self._lock:
            return [item.model_copy(deep=True) for item in list(self._plans.values())[-limit:]][::-1]

    def _transition(self, deployment_id: UUID, change) -> AppDeployment:
        with self._lock:
            plan = self.get(deployment_id)
            job = change(plan)
            self._plans[deployment_id] = plan
            if job:
                self._jobs[job.id] = job
            return plan.model_copy(deep=True)

    def approve(self, deployment_id: UUID) -> AppDeployment:
        return self._transition(deployment_id, _approve)

    def request_delete(self, deployment_id: UUID) -> AppDeployment:
        return self._transition(deployment_id, _delete)

    def set_status(self, deployment_id: UUID, status: DeploymentStatus, **changes) -> AppDeployment:
        with self._lock:
            plan = self.get(deployment_id)
            plan.status = status
            plan.updated_at = datetime.now(timezone.utc)
            for key, value in changes.items():
                setattr(plan, key, value)
            self._plans[deployment_id] = plan
            return plan.model_copy(deep=True)

    def pending_jobs(self, limit: int = 20) -> list[AppJob]:
        with self._lock:
            return [job for job_id, job in self._jobs.items() if job_id not in self._sent][:limit]

    def mark_sent(self, job_id: UUID) -> None:
        with self._lock:
            self._sent.add(job_id)


class DurableAppStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def create(self, plan: AppDeployment) -> AppDeployment:
        with Session(self.engine) as session, session.begin():
            session.add(AppDeploymentRow(id=str(plan.id), data=plan.model_dump(mode="json")))
        return plan

    def get(self, deployment_id: UUID) -> AppDeployment:
        with Session(self.engine) as session:
            row = session.get(AppDeploymentRow, str(deployment_id))
            if row is None:
                raise AppNotFound
            return AppDeployment.model_validate(row.data)

    def list_deployments(self, limit: int = 50) -> list[AppDeployment]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(AppDeploymentRow)
                .order_by(AppDeploymentRow.data["created_at"].as_string().desc(), AppDeploymentRow.id.desc())
                .limit(limit)
            ).all()
            return [AppDeployment.model_validate(row.data) for row in rows]

    def _transition(self, deployment_id: UUID, change) -> AppDeployment:
        with Session(self.engine) as session, session.begin():
            row = session.execute(
                select(AppDeploymentRow).where(AppDeploymentRow.id == str(deployment_id)).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise AppNotFound
            plan = AppDeployment.model_validate(row.data)
            job = change(plan)
            row.data = plan.model_dump(mode="json")
            if job:
                session.add(AppJobOutboxRow(
                    id=str(job.id), deployment_id=str(plan.id), data=job.model_dump(mode="json")
                ))
        return plan

    def approve(self, deployment_id: UUID) -> AppDeployment:
        return self._transition(deployment_id, _approve)

    def request_delete(self, deployment_id: UUID) -> AppDeployment:
        return self._transition(deployment_id, _delete)

    def set_status(self, deployment_id: UUID, status: DeploymentStatus, **changes) -> AppDeployment:
        def update(plan: AppDeployment) -> None:
            plan.status = status
            plan.updated_at = datetime.now(timezone.utc)
            for key, value in changes.items():
                setattr(plan, key, value)
        return self._transition(deployment_id, update)

    def pending_jobs(self, limit: int = 20) -> list[AppJob]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(AppJobOutboxRow).where(AppJobOutboxRow.sent_at.is_(None))
                .order_by(AppJobOutboxRow.id).limit(limit)
            ).all()
            return [AppJob.model_validate(row.data) for row in rows]

    def mark_sent(self, job_id: UUID) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(AppJobOutboxRow, str(job_id))
            if row is not None and row.sent_at is None:
                row.sent_at = datetime.now(timezone.utc)
