"""Git-backed Code Agent. Applying a model patch requires explicit opt-in."""

import subprocess
from pathlib import Path
from typing import Protocol

from agents_shared.models import WorkflowTask


class PatchModel(Protocol):
    def generate_patch(self, instruction: str, context: str) -> str: ...


class OpenAIPatchModel:
    def __init__(self, model: str) -> None:
        from openai import OpenAI

        self.client = OpenAI()
        self.model = model

    def generate_patch(self, instruction: str, context: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            instructions=(
                "Return only a Git-compatible unified diff. Use repository-relative paths. "
                "Do not include Markdown fences or explanations."
            ),
            input=f"Task:\n{instruction}\n\nTracked source files:\n{context}",
        )
        return response.output_text


class CodeAgent:
    def __init__(self, repository: Path, model: PatchModel, *, apply_changes: bool = False) -> None:
        self.repository = repository.resolve()
        self.model = model
        self.apply_changes = apply_changes
        self._git("rev-parse", "--show-toplevel")

    def _git(self, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repository), *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Git command failed")
        return result.stdout

    def _source_context(self) -> str:
        paths = self._git("ls-files", "-z").split("\0")
        parts: list[str] = []
        remaining = 50_000
        for name in paths:
            if not name or not (name.endswith(".py") or name == "pyproject.toml"):
                continue
            path = self.repository / name
            if path.is_symlink() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            entry = f"--- {name} ---\n{content}\n"
            if len(entry) > remaining:
                break
            parts.append(entry)
            remaining -= len(entry)
        return "\n".join(parts)

    def execute(self, task: WorkflowTask) -> str:
        if self._git("status", "--porcelain"):
            raise RuntimeError("Repository must be clean before generating a patch")
        patch = self.model.generate_patch(task.instruction, self._source_context())
        if not patch.startswith("diff --git "):
            raise ValueError("Model did not return a Git patch")
        self._git("apply", "--check", "-", input_text=patch)
        if self.apply_changes:
            self._git("apply", "-", input_text=patch)
            return "Patch applied to working tree; review and test before committing"
        return patch
