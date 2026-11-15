"""Create an isolated checkout for a Code or Security Agent task."""

import os
import subprocess
from pathlib import Path

from agents_shared.models import GitHubSourceTarget


def checkout_source(target: GitHubSourceTarget, destination: Path) -> None:
    url = f"https://github.com/{target.owner}/{target.repo}.git"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if env.get("GITHUB_TOKEN"):
        env["GIT_ASKPASS"] = env.get("GIT_ASKPASS_PATH", "/app/scripts/git_askpass.py")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--single-branch", "--branch", target.ref, url, str(destination)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
