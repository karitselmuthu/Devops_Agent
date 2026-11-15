import subprocess
from pathlib import Path

import pytest

from agents.code_agent import CodeAgent
from agents_platform.worker import run_forever
from agents_shared.messaging import InMemoryTaskBroker
from agents_shared.models import AgentKind, WorkflowTask


PATCH = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1 +1 @@
-print('before')
+print('after')
"""


class FakeModel:
    def generate_patch(self, instruction: str, context: str) -> str:
        assert instruction == "Change example"
        assert "print('before')" in context
        return PATCH


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_code_agent_previews_then_applies_valid_patch(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    source = tmp_path / "example.py"
    source.write_text("print('before')\n")
    git(tmp_path, "add", "example.py")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
    task = WorkflowTask(agent=AgentKind.CODE, instruction="Change example")

    assert CodeAgent(tmp_path, FakeModel()).execute(task) == PATCH
    assert source.read_text() == "print('before')\n"
    assert "Patch applied" in CodeAgent(tmp_path, FakeModel(), apply_changes=True).execute(task)
    assert source.read_text() == "print('after')\n"
    with pytest.raises(RuntimeError, match="clean"):
        CodeAgent(tmp_path, FakeModel()).execute(task)


def test_worker_loop_rejects_nonpositive_poll_interval() -> None:
    from threading import Event

    with pytest.raises(ValueError, match="positive"):
        run_forever(InMemoryTaskBroker(), AgentKind.CODE, lambda task: "ok", Event(), 0)
