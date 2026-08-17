"""Configuration: paths, limits, and the repository allowlist."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
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

# Loop-detection knobs: max attempts per phase and the thresholds that classify
# repeated work as a loop.
PHASE_MAX_ATTEMPTS = int(os.environ.get("ORCHESTRATOR_PHASE_MAX_ATTEMPTS", "2"))
LOOP_REPEAT_THRESHOLD = int(os.environ.get("ORCHESTRATOR_LOOP_REPEAT_THRESHOLD", "20"))
LOOP_REPEAT_WINDOW = int(os.environ.get("ORCHESTRATOR_LOOP_REPEAT_WINDOW", "100"))
LOOP_RATIO_THRESHOLD = float(os.environ.get("ORCHESTRATOR_LOOP_RATIO_THRESHOLD", "0.1"))
LOOP_CHECK_INTERVAL = int(os.environ.get("ORCHESTRATOR_LOOP_CHECK_INTERVAL", "25"))

SKILL_SUBAGENT_PLAN_EXECUTION = os.environ.get(
    "ORCHESTRATOR_SKILL_SUBAGENT_PLAN_EXECUTION",
    "/home/bruno/.agents/skills/subagent-plan-execution",
)


@dataclass
class ModelConfig:
    """Model selection for a single role (primary or fallback)."""

    name: str | None
    variant: str | None


def _parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def load_fallback_enabled() -> bool:
    env_value = os.environ.get("ORCHESTRATOR_MODEL_FALLBACK_ENABLED")
    if env_value is not None:
        return _parse_bool(env_value)
    if not CONFIG_FILE.exists():
        return False
    with CONFIG_FILE.open() as fh:
        data = yaml.safe_load(fh) or {}
    return _parse_bool((data.get("model") or {}).get("fallback_enabled"))


@lru_cache(maxsize=1)
def load_model_config() -> dict[str, ModelConfig | None]:
    """Load the model: section from the config file. Returns {"primary": ..., "fallback": ...}."""
    if not CONFIG_FILE.exists():
        data = {}
    else:
        with CONFIG_FILE.open() as fh:
            data = yaml.safe_load(fh) or {}
    result: dict[str, ModelConfig | None] = {}
    for role in ("primary", "fallback"):
        prefix = f"ORCHESTRATOR_MODEL_{role.upper()}"
        name = os.environ.get(f"{prefix}_NAME")
        variant = os.environ.get(f"{prefix}_VARIANT")
        entry = (data.get("model") or {}).get(role) or {}
        if name is None:
            name = entry.get("name")
        if variant is None:
            variant = entry.get("variant")
        result[role] = ModelConfig(name=name, variant=variant) if name or variant else None
    return result


MODEL_PRIMARY = load_model_config().get("primary")
MODEL_FALLBACK = load_model_config().get("fallback")
MODEL_FALLBACK_ENABLED = load_fallback_enabled()


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
