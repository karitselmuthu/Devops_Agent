from uuid import UUID

from agents_shared.messaging import TaskBroker
from agents_shared.models import (
    TaskResultMessage,
    TaskStatus,
    WorkflowPlan,
    WorkflowRequest,
    WorkflowTaskMessage,
)
from planning_agent.planner import create_plan


class WorkflowNotFound(Exception):
    pass


class InvalidTaskResult(Exception):
    pass


class Orchestrator:
    """Sequential workflow coordinator with replaceable broker and storage."""

    def __init__(self, broker: TaskBroker) -> None:
        self.broker = broker
        self._workflows: dict[UUID, WorkflowPlan] = {}

    def create(self, request: WorkflowRequest) -> WorkflowPlan:
        plan = create_plan(request)
        self._workflows[plan.id] = plan
        self._queue_next(plan)
        return plan.model_copy(deep=True)

    def get(self, workflow_id: UUID) -> WorkflowPlan:
        try:
            return self._workflows[workflow_id].model_copy(deep=True)
        except KeyError as exc:
            raise WorkflowNotFound from exc

    def record_result(self, result: TaskResultMessage) -> WorkflowPlan:
        if result.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise InvalidTaskResult("A result must be COMPLETED or FAILED")
        try:
            plan = self._workflows[result.workflow_id]
        except KeyError as exc:
            raise WorkflowNotFound from exc
        task = next((item for item in plan.tasks if item.id == result.task_id), None)
        if task is None or task.status != TaskStatus.QUEUED:
            raise InvalidTaskResult("Task does not exist or is not queued")
        task.status = result.status
        task.output = result.output
        if result.status == TaskStatus.COMPLETED:
            self._queue_next(plan)
        return plan.model_copy(deep=True)

    def process_next_result(self) -> WorkflowPlan | None:
        result = self.broker.receive_result()
        return self.record_result(result) if result is not None else None

    def _queue_next(self, plan: WorkflowPlan) -> None:
        task = next((item for item in plan.tasks if item.status == TaskStatus.PENDING), None)
        if task is None:
            return
        task.status = TaskStatus.QUEUED
        self.broker.publish(WorkflowTaskMessage(workflow_id=plan.id, task=task.model_copy(deep=True)))
