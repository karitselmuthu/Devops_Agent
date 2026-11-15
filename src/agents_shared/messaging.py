from collections import defaultdict, deque
from typing import Protocol

from agents_shared.models import AgentKind, TaskResultMessage, WorkflowTaskMessage


class TaskBroker(Protocol):
    def publish(self, message: WorkflowTaskMessage) -> None: ...

    def receive(self, agent: AgentKind) -> WorkflowTaskMessage | None: ...

    def publish_result(self, result: TaskResultMessage) -> None: ...

    def receive_result(self) -> TaskResultMessage | None: ...


class InMemoryTaskBroker:
    """Single-process development broker. Messages vanish on restart."""

    def __init__(self) -> None:
        self._queues: dict[AgentKind, deque[WorkflowTaskMessage]] = defaultdict(deque)
        self._results: deque[TaskResultMessage] = deque()

    def publish(self, message: WorkflowTaskMessage) -> None:
        self._queues[message.task.agent].append(message)

    def receive(self, agent: AgentKind) -> WorkflowTaskMessage | None:
        queue = self._queues[agent]
        return queue.popleft() if queue else None

    def publish_result(self, result: TaskResultMessage) -> None:
        self._results.append(result)

    def receive_result(self) -> TaskResultMessage | None:
        return self._results.popleft() if self._results else None
