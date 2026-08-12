"""Configuration: paths, limits, and the repository allowlist."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("ORCHESTRATOR_DATA_DIR", REPO_ROOT / "data"))
STATE_DIR = DATA_DIR / "state"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_FILE = Path(os.environ.get("ORCHESTRATOR_CONFIG_FILE", REPO_ROOT / "config" / "repositories.yaml"))

REPOS_DIR = Path(os.environ.get("ORCHESTRATOR_REPOS_DIR", Path.home() / "agent-repos"))
WORKSPACES_DIR = Path(os.environ.get("ORCHESTRATOR_WORKSPACES_DIR", Path.home() / "agent-workspaces"))


def _find_opencode() -> str:
    """Resolve the opencode binary: PATH lookup first, then known install locations."""
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".local" / "bin" / "opencode",
    ):
        if candidate.is_file():
            return str(candidate)
    return "opencode"


OPENCODE_BIN = os.environ.get("ORCHESTRATOR_OPENCODE_BIN") or _find_opencode()
OPENCODE_TIMEOUT_SECONDS = int(os.environ.get("ORCHESTRATOR_OPENCODE_TIMEOUT", str(60 * 60)))
POLL_INTERVAL_SECONDS = int(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", str(5 * 60)))
MAX_CONCURRENT_TASKS = int(os.environ.get("ORCHESTRATOR_MAX_CONCURRENT", "1"))
# A task/comment with no activity for this long is considered dead (process died).
STALE_SECONDS = int(os.environ.get("ORCHESTRATOR_STALE_SECONDS", str(2 * 60 * 60)))

SKILL_SUBAGENT_PLAN_EXECUTION = os.environ.get(
    "ORCHESTRATOR_SKILL_SUBAGENT_PLAN_EXECUTION",
    "/home/bruno/.agents/skills/subagent-plan-execution",
)


@lru_cache(maxsize=1)
def load_repository_config() -> dict[str, dict]:
    """Load config/repositories.yaml. Returns {name: {options}}."""
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open() as fh:
        data = yaml.safe_load(fh) or {}
    result: dict[str, dict] = {}
    for entry in data.get("repositories", []):
        name = entry.get("name")
        if name:
            result[name] = {k: v for k, v in entry.items() if k != "name"}
    return result


def is_repository_allowed(repository: str) -> bool:
    return repository in load_repository_config()


def repository_label(repository: str) -> str | None:
    """Label filter for the repo (issues must carry it to be picked up by poll)."""
    return load_repository_config().get(repository, {}).get("label")


def repository_command(repository: str) -> str:
    """Comment prefix that triggers a re-run for the repo (default /ai-agent)."""
    return load_repository_config().get(repository, {}).get("command") or "/ai-agent"


def allowed_repositories() -> list[str]:
    return sorted(load_repository_config())