"""Configuration: paths, limits, and the repository allowlist."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from orchestrator.main.providers import (
    DESTINATION_PROVIDERS,
    EXECUTOR_PROVIDERS,
    INPUT_PROVIDERS,
    WORKSPACE_PROVIDERS,
    REVIEW_DESTINATION_PROVIDERS,
    REVIEW_EXECUTOR_PROVIDERS,
    REVIEW_INPUT_PROVIDERS,
    REVIEW_WORKSPACE_PROVIDERS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"


def _environment_path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def load_environment() -> bool:
    """Load local deployment overrides without replacing exported variables."""
    if os.environ.get("ORCHESTRATOR_LOAD_DOTENV", "1") != "1":
        return False
    return load_dotenv(ENV_FILE, override=False)


load_environment()

DATA_DIR = _environment_path("ORCHESTRATOR_DATA_DIR", REPO_ROOT / "data")
STATE_DIR = DATA_DIR / "state"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_FILE = _environment_path("ORCHESTRATOR_CONFIG_FILE", REPO_ROOT / "config" / "config.yaml")

REPOS_DIR = _environment_path("ORCHESTRATOR_REPOS_DIR", Path.home() / "agent-repos")
WORKSPACES_DIR = _environment_path("ORCHESTRATOR_WORKSPACES_DIR", Path.home() / "agent-workspaces")


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


@dataclass
class ModelConfig:
    """Model selection for OpenCode runs."""

    name: str | None
    variant: str | None


@dataclass(frozen=True)
class ProviderConfig:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True)
class PipelineConfig:
    execution: "ExecutionPipelineConfig"
    review: "ReviewPipelineConfig"


@dataclass(frozen=True)
class ExecutionPipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    workspace_manager: ProviderConfig
    destination: ProviderConfig


@dataclass(frozen=True)
class ReviewPipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    workspace_manager: ProviderConfig
    destination: ProviderConfig


_PROVIDER_DEFAULTS = {
    "input_source": "github_polling",
    "executor": "opencode",
    "workspace_manager": "git",
    "destination": "github",
}


def _provider_config(
    key: str, pipeline: dict[str, Any], registry: ProviderRegistry
) -> ProviderConfig:
    raw = pipeline.get(key) or {}
    if isinstance(raw, str):
        provider_type, options = raw, {}
    else:
        provider_type = raw.get("type", _PROVIDER_DEFAULTS[key])
        options = {k: v for k, v in raw.items() if k != "type"}
    registry.get(provider_type)
    return ProviderConfig(type=provider_type, options=options)


def _execution_pipeline_config(pipeline: dict[str, Any]) -> ExecutionPipelineConfig:
    return ExecutionPipelineConfig(
        input_source=_provider_config("input_source", pipeline, INPUT_PROVIDERS),
        executor=_provider_config("executor", pipeline, EXECUTOR_PROVIDERS),
        workspace_manager=_provider_config("workspace_manager", pipeline, WORKSPACE_PROVIDERS),
        destination=_provider_config("destination", pipeline, DESTINATION_PROVIDERS),
    )


def _review_pipeline_config(pipeline: dict[str, Any]) -> ReviewPipelineConfig:
    return ReviewPipelineConfig(
        input_source=_provider_config("input_source", pipeline, REVIEW_INPUT_PROVIDERS),
        executor=_provider_config("executor", pipeline, REVIEW_EXECUTOR_PROVIDERS),
        workspace_manager=_provider_config("workspace_manager", pipeline, REVIEW_WORKSPACE_PROVIDERS),
        destination=_provider_config("destination", pipeline, REVIEW_DESTINATION_PROVIDERS),
    )


@lru_cache(maxsize=1)
def load_pipeline_config() -> PipelineConfig:
    """Load provider selections from the optional ``pipeline`` config section."""
    if not CONFIG_FILE.exists():
        data = {}
    else:
        with CONFIG_FILE.open() as fh:
            data = yaml.safe_load(fh) or {}
    pipeline = data.get("pipeline") or {}
    return PipelineConfig(
        execution=_execution_pipeline_config(pipeline.get("execution") or {}),
        review=_review_pipeline_config(pipeline.get("review") or {}),
    )


def load_review_pipeline_config() -> ReviewPipelineConfig:
    """Return the review provider configuration from the main pipeline config."""
    return load_pipeline_config().review


# Both sections are cached together, so preserve a review-specific cache-clear
# hook without allowing a stale review configuration.
load_review_pipeline_config.cache_clear = load_pipeline_config.cache_clear  # type: ignore[attr-defined]


def _load_model_config(section: str) -> ModelConfig | None:
    """Load one independently configured OpenCode model, if any."""
    if not CONFIG_FILE.exists():
        data = {}
    else:
        with CONFIG_FILE.open() as fh:
            data = yaml.safe_load(fh) or {}
    entry = (data.get("model") or {}).get(section) or {}
    variable_prefix = f"ORCHESTRATOR_MODEL_{section.upper()}"
    name = os.environ.get(f"{variable_prefix}_NAME", entry.get("name"))
    variant = os.environ.get(f"{variable_prefix}_VARIANT", entry.get("variant"))
    return ModelConfig(name=name, variant=variant) if name or variant else None


@lru_cache(maxsize=1)
def load_execution_model_config() -> ModelConfig | None:
    return _load_model_config("execution")


@lru_cache(maxsize=1)
def load_review_model_config() -> ModelConfig | None:
    return _load_model_config("review")


@lru_cache(maxsize=1)
def load_repository_config() -> dict[str, dict]:
    """Load config/config.yaml. Returns {name: {options}}."""
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
