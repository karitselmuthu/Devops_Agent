from collections.abc import Callable
from threading import Event

from agents_shared.messaging import TaskBroker
from agents_shared.models import AgentKind, TaskResultMessage, TaskStatus, WorkflowTask
from agents_shared.results import bounded_result_output


def run_once(
    broker: TaskBroker, agent: AgentKind, execute: Callable[[WorkflowTask], str]
) -> bool:
    """Consume one task and publish a result; the caller owns the worker loop."""
    message = broker.receive(agent)
    if message is None:
        return False
    try:
        output = execute(message.task)
        status = TaskStatus.COMPLETED
    except Exception as exc:
        output = str(exc)
        status = TaskStatus.FAILED
    broker.publish_result(
        TaskResultMessage(
            workflow_id=message.workflow_id,
            task_id=message.task.id,
            status=status,
            output=bounded_result_output(output),
        )
    )
    return True


def run_forever(
    broker: TaskBroker,
    agent: AgentKind,
    execute: Callable[[WorkflowTask], str],
    stop: Event,
    poll_interval: float = 1.0,
) -> None:
    """Run a single agent until stopped by the hosting process."""
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    while not stop.is_set():
        if not run_once(broker, agent, execute):
            stop.wait(poll_interval)
