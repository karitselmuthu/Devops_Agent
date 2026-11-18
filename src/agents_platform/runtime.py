"""AWS worker entry points and dependency wiring."""

import argparse
import os
import signal
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from tempfile import TemporaryDirectory

from agents.code_agent import CodeAgent, OpenAIPatchModel
from agents.devops_agent import CodePipelineProvider, DevOpsAgent, GitHubActionsProvider
from agents.infrastructure_agent import InfrastructureAgent
from agents.monitoring_agent import MonitoringAgent
from agents.security_agent import SecurityAgent
from agents_platform.persistence import DurableWorkflowStore
from agents_platform.config import database_url
from agents_platform.source import checkout_source
from agents_platform.app_store import DurableAppStore
from agents_platform.app_runtime import AppJobQueue, dispatch_app_jobs_once
from agents_shared.models import AgentKind, TaskResultMessage, TaskStatus, WorkflowTask
from agents_shared.sqs import SqsTaskBroker
from agents_shared.results import bounded_result_output


def aws_components() -> tuple[DurableWorkflowStore, SqsTaskBroker]:
    queue_urls = {agent: os.environ[f"SQS_{agent}_URL"] for agent in AgentKind}
    broker = SqsTaskBroker(queue_urls, os.environ["SQS_RESULTS_URL"])
    return DurableWorkflowStore(database_url()), broker


def make_agent(agent: AgentKind) -> Callable[[WorkflowTask], str]:
    if agent == AgentKind.CODE:
        model = OpenAIPatchModel(os.environ["OPENAI_MODEL"])

        def code_task(task: WorkflowTask) -> str:
            if task.source_target is None:
                raise ValueError("Code task requires a source target")
            with TemporaryDirectory() as workspace:
                repository = Path(workspace) / "source"
                checkout_source(task.source_target, repository)
                return CodeAgent(repository, model).execute(task)

        return code_task
    if agent == AgentKind.SECURITY:
        def security_task(task: WorkflowTask) -> str:
            if task.source_target is None:
                raise ValueError("Security task requires a source target")
            with TemporaryDirectory() as workspace:
                repository = Path(workspace) / "source"
                checkout_source(task.source_target, repository)
                return SecurityAgent(repository).execute(task)

        return security_task
    if agent == AgentKind.DEVOPS:
        github_token = os.environ.get("GITHUB_TOKEN")
        return DevOpsAgent(
            github=GitHubActionsProvider(github_token) if github_token else None,
            codepipeline=CodePipelineProvider(),
        ).execute
    if agent == AgentKind.INFRA:
        return InfrastructureAgent().execute
    return MonitoringAgent().execute


def dispatch_once(store: DurableWorkflowStore, broker: SqsTaskBroker) -> int:
    messages = store.pending_outbox()
    for message in messages:
        broker.publish(message)
        store.mark_sent(message.task.id)
    return len(messages)


def result_once(store: DurableWorkflowStore, broker: SqsTaskBroker) -> bool:
    delivery = broker.receive_result_delivery()
    if delivery is None:
        return False
    store.apply_result(delivery.message)
    delivery.ack()
    return True


def agent_once(
    store: DurableWorkflowStore,
    broker: SqsTaskBroker,
    agent: AgentKind,
    execute: Callable[[WorkflowTask], str],
) -> bool:
    delivery = broker.receive_task_delivery(agent)
    if delivery is None:
        return False
    message = delivery.message
    saved = store.claimed_result(message.task.id)
    if saved is not None:
        broker.publish_result(saved)
        delivery.ack()
        return True
    if not store.claim_task(message.task.id):
        return False

    stop_heartbeat = Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(60):
            delivery.extend_visibility()

    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        try:
            output = execute(message.task)
            status = TaskStatus.COMPLETED
        except Exception as exc:
            output = str(exc)
            status = TaskStatus.FAILED
        result = TaskResultMessage(
            workflow_id=message.workflow_id,
            task_id=message.task.id,
            status=status,
            output=bounded_result_output(output),
        )
        store.save_claimed_result(result)
        broker.publish_result(result)
        delivery.ack()
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=5)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("dispatcher", "results", "worker"))
    parser.add_argument("--agent", choices=[agent.value for agent in AgentKind])
    args = parser.parse_args()
    if args.role == "worker" and args.agent is None:
        parser.error("--agent is required for worker")
    store, broker = aws_components()
    app_store = DurableAppStore(database_url()) if args.role == "dispatcher" else None
    app_queue = AppJobQueue(os.environ["SQS_APP_DEPLOY_URL"]) if args.role == "dispatcher" else None
    agent = AgentKind(args.agent) if args.agent else None
    execute = make_agent(agent) if agent else None
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        if args.role == "dispatcher":
            count = dispatch_once(store, broker) + dispatch_app_jobs_once(app_store, app_queue)
            if count == 0:
                stop.wait(1)
        elif args.role == "results":
            result_once(store, broker)
        else:
            agent_once(store, broker, agent, execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
