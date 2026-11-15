"""PostgreSQL workflow state and a transactional task outbox."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, String, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from agents_platform.orchestrator import InvalidTaskResult, WorkflowNotFound
from agents_shared.models import (
    TaskResultMessage,
    TaskStatus,
    WorkflowPlan,
    WorkflowRequest,
    WorkflowTask,
    WorkflowTaskMessage,
)
from planning_agent.planner import create_plan


class Base(DeclarativeBase):
    pass


class WorkflowRow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class OutboxRow(Base):
    __tablename__ = "task_outbox"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(36), ForeignKey("workflows.id"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskClaimRow(Base):
    __tablename__ = "task_claims"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)


class DurableWorkflowStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)

    @staticmethod
    def _queue(session: Session, plan: WorkflowPlan, task: WorkflowTask) -> None:
        task.status = TaskStatus.QUEUED
        message = WorkflowTaskMessage(workflow_id=plan.id, task=task.model_copy(deep=True))
        session.add(
            OutboxRow(
                task_id=str(task.id),
                workflow_id=str(plan.id),
                payload=message.model_dump(mode="json"),
            )
        )

    def create(self, request: WorkflowRequest) -> WorkflowPlan:
        plan = create_plan(request)
        with Session(self.engine) as session, session.begin():
            self._queue(session, plan, plan.tasks[0])
            session.add(WorkflowRow(id=str(plan.id), data=plan.model_dump(mode="json")))
        return plan

    def get(self, workflow_id: UUID) -> WorkflowPlan:
        with Session(self.engine) as session:
            row = session.get(WorkflowRow, str(workflow_id))
            if row is None:
                raise WorkflowNotFound
            return WorkflowPlan.model_validate(row.data)

    def apply_result(self, result: TaskResultMessage) -> WorkflowPlan:
        if result.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise InvalidTaskResult("A result must be COMPLETED or FAILED")
        with Session(self.engine) as session, session.begin():
            row = session.execute(
                select(WorkflowRow).where(WorkflowRow.id == str(result.workflow_id)).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise WorkflowNotFound
            plan = WorkflowPlan.model_validate(row.data)
            task = next((item for item in plan.tasks if item.id == result.task_id), None)
            if task is None:
                raise InvalidTaskResult("Unknown task")
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                if task.status == result.status and task.output == result.output:
                    return plan
                raise InvalidTaskResult("Conflicting result for completed task")
            if task.status != TaskStatus.QUEUED:
                raise InvalidTaskResult("Task is not queued")
            task.status = result.status
            task.output = result.output
            if result.status == TaskStatus.COMPLETED:
                next_task = next((item for item in plan.tasks if item.status == TaskStatus.PENDING), None)
                if next_task is not None:
                    self._queue(session, plan, next_task)
            row.data = plan.model_dump(mode="json")
        return plan

    def pending_outbox(self, limit: int = 20) -> list[WorkflowTaskMessage]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(OutboxRow)
                .where(OutboxRow.sent_at.is_(None))
                .order_by(OutboxRow.task_id)
                .limit(limit)
            ).all()
            return [WorkflowTaskMessage.model_validate(row.payload) for row in rows]

    def mark_sent(self, task_id: UUID) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(OutboxRow, str(task_id))
            if row is not None and row.sent_at is None:
                row.sent_at = datetime.now(timezone.utc)

    def claim_task(self, task_id: UUID) -> bool:
        try:
            with Session(self.engine) as session, session.begin():
                session.add(TaskClaimRow(task_id=str(task_id), claimed_at=datetime.now(timezone.utc)))
            return True
        except IntegrityError:
            return False

    def claimed_result(self, task_id: UUID) -> TaskResultMessage | None:
        with Session(self.engine) as session:
            row = session.get(TaskClaimRow, str(task_id))
            return TaskResultMessage.model_validate(row.result) if row and row.result else None

    def save_claimed_result(self, result: TaskResultMessage) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(TaskClaimRow, str(result.task_id))
            if row is None:
                raise InvalidTaskResult("Task was not claimed")
            row.result = result.model_dump(mode="json")
