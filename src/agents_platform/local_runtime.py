"""Run the in-memory agent queues in the development API process."""

from threading import Event

from agents_platform.orchestrator import Orchestrator
from agents_platform.runtime import make_agent
from agents_platform.worker import run_once
from agents_shared.models import AgentKind


def run_local_once(orchestrator: Orchestrator) -> bool:
    handled = False
    for agent in AgentKind:
        if run_once(orchestrator.broker, agent, lambda task: make_agent(agent)(task)):
            orchestrator.process_next_result()
            handled = True
    return handled


def run_local_forever(orchestrator: Orchestrator, stop: Event) -> None:
    while not stop.is_set():
        if not run_local_once(orchestrator):
            stop.wait(0.5)
