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
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--branch", target.ref, url, str(destination)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub checkout timed out. Check network access and try again.") from exc
    if result.returncode == 0:
        return
    error = result.stderr.lower()
    if "remote branch" in error and "not found" in error:
        raise RuntimeError(
            f"Branch '{target.ref}' does not exist in {target.owner}/{target.repo}. "
            "Select a branch that exists in the repository."
        )
    if "repository not found" in error or "authentication failed" in error or "could not read username" in error:
        raise RuntimeError("GitHub repository not found or access denied. Check the URL and GitHub token.")
    if "could not resolve host" in error or "failed to connect" in error:
        raise RuntimeError("Cannot reach GitHub. Check your network or DNS connection.")
    raise RuntimeError(f"GitHub checkout failed (git exit {result.returncode}). Check repository access and branch.")
