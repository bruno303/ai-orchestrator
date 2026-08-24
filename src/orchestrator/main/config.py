"""Configuration: paths, limits, and the repository allowlist."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
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
    TRIAGE_DESTINATION_PROVIDERS,
    TRIAGE_EXECUTOR_PROVIDERS,
    TRIAGE_INPUT_PROVIDERS,
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
    """Model selection for agent runs."""

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
    triage: "TriagePipelineConfig"


@dataclass(frozen=True)
class ExecutionPipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    workspace_manager: ProviderConfig
    destination: ProviderConfig
    labels: "StageLabelConfig" = field(default_factory=lambda: _default_stage_labels("execution"))


@dataclass(frozen=True)
class ReviewPipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    workspace_manager: ProviderConfig
    destination: ProviderConfig
    labels: "StageLabelConfig" = field(default_factory=lambda: _default_stage_labels("review"))


@dataclass(frozen=True)
class TriagePipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    destination: ProviderConfig
    labels: "TriageLabelConfig" = field(default_factory=lambda: _default_triage_labels())


_PROVIDER_DEFAULTS = {
    "input_source": "github_polling",
    "executor": "opencode",
    "workspace_manager": "git",
    "destination": "github",
}


# These labels are workflow contracts, rather than repository-specific
# settings.  Keep the names in one place so every provider receives the same
# durable hand-off semantics.
EXECUTION_READY_LABEL = "ai-agent"
TRIAGE_BLOCKED_LABEL = "ai-triage"
EXECUTION_COMPLETED_LABEL = "ai-developed"
REVIEW_COMPLETED_LABEL = "ai-reviewed"

# More descriptive aliases for callers that prefer the stage terminology.
DEFAULT_EXECUTION_READY_LABEL = EXECUTION_READY_LABEL
DEFAULT_TRIAGE_BLOCKED_LABEL = TRIAGE_BLOCKED_LABEL
DEFAULT_EXECUTION_COMPLETED_LABEL = EXECUTION_COMPLETED_LABEL
DEFAULT_REVIEW_COMPLETED_LABEL = REVIEW_COMPLETED_LABEL


@dataclass(frozen=True)
class LabelOutputConfig:
    """Labels added and removed after a stage publishes its result."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageLabelConfig:
    """Input and successful-output label contract for one stage."""

    select: tuple[str, ...] = ()
    suppress: tuple[str, ...] = ()
    output: LabelOutputConfig = field(default_factory=LabelOutputConfig)


@dataclass(frozen=True)
class TriageOutputConfig:
    ready: LabelOutputConfig = field(default_factory=LabelOutputConfig)
    blocked: LabelOutputConfig = field(default_factory=LabelOutputConfig)


@dataclass(frozen=True)
class TriageLabelConfig:
    select: tuple[str, ...] = ()
    suppress: tuple[str, ...] = ()
    output: TriageOutputConfig = field(default_factory=TriageOutputConfig)


def _default_stage_labels(stage: str) -> StageLabelConfig:
    if stage == "execution":
        return StageLabelConfig(
            select=(EXECUTION_READY_LABEL,),
            suppress=(EXECUTION_COMPLETED_LABEL,),
            output=LabelOutputConfig(add=(EXECUTION_COMPLETED_LABEL,)),
        )
    if stage == "review":
        return StageLabelConfig(
            suppress=(REVIEW_COMPLETED_LABEL,),
            output=LabelOutputConfig(add=(REVIEW_COMPLETED_LABEL,)),
        )
    raise ValueError(f"unknown stage label defaults: {stage}")


def _default_triage_labels() -> TriageLabelConfig:
    return TriageLabelConfig(
        suppress=(
            EXECUTION_READY_LABEL,
            TRIAGE_BLOCKED_LABEL,
            EXECUTION_COMPLETED_LABEL,
        ),
        output=TriageOutputConfig(
            ready=LabelOutputConfig(
                add=(EXECUTION_READY_LABEL,),
                remove=(TRIAGE_BLOCKED_LABEL,),
            ),
            blocked=LabelOutputConfig(add=(TRIAGE_BLOCKED_LABEL,)),
        ),
    )


def _labels(value: Any, *, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be a list of label names")
    labels: list[str] = []
    for label in value:
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"{path} must contain only non-empty label names")
        labels.append(label.strip())
    return tuple(dict.fromkeys(labels))


def _label_output(value: Any, default: LabelOutputConfig, *, path: str) -> LabelOutputConfig:
    if value is None:
        return default
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping with add/remove label lists")
    return LabelOutputConfig(
        add=_labels(value.get("add", default.add), path=f"{path}.add"),
        remove=_labels(value.get("remove", default.remove), path=f"{path}.remove"),
    )


def _stage_labels(pipeline: dict[str, Any], stage: str) -> StageLabelConfig:
    default = _default_stage_labels(stage)
    raw = pipeline.get("labels") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"pipeline.{stage}.labels must be a mapping")
    return StageLabelConfig(
        select=_labels(raw.get("select", default.select), path=f"pipeline.{stage}.labels.select"),
        suppress=_labels(raw.get("suppress", default.suppress), path=f"pipeline.{stage}.labels.suppress"),
        output=_label_output(
            raw.get("output"), default.output, path=f"pipeline.{stage}.labels.output"
        ),
    )


def _triage_labels(pipeline: dict[str, Any]) -> TriageLabelConfig:
    default = _default_triage_labels()
    raw = pipeline.get("labels") or {}
    if not isinstance(raw, dict):
        raise ValueError("pipeline.triage.labels must be a mapping")
    output = raw.get("output") or {}
    if not isinstance(output, dict):
        raise ValueError("pipeline.triage.labels.output must be a mapping")
    return TriageLabelConfig(
        select=_labels(raw.get("select", default.select), path="pipeline.triage.labels.select"),
        suppress=_labels(raw.get("suppress", default.suppress), path="pipeline.triage.labels.suppress"),
        output=TriageOutputConfig(
            ready=_label_output(
                output.get("ready"), default.output.ready,
                path="pipeline.triage.labels.output.ready",
            ),
            blocked=_label_output(
                output.get("blocked"), default.output.blocked,
                path="pipeline.triage.labels.output.blocked",
            ),
        ),
    )


def _provider_config(
    key: str,
    pipeline: dict[str, Any],
    registry: ProviderRegistry,
    environment_variable: str | None = None,
) -> ProviderConfig:
    raw = pipeline.get(key) or {}
    if isinstance(raw, str):
        provider_type, options = raw, {}
    else:
        provider_type = raw.get("type", _PROVIDER_DEFAULTS[key])
        options = {k: v for k, v in raw.items() if k != "type"}
    if environment_variable is not None:
        provider_type = os.environ.get(environment_variable, provider_type)
    registry.get(provider_type)
    return ProviderConfig(type=provider_type, options=options)


def _execution_pipeline_config(pipeline: dict[str, Any]) -> ExecutionPipelineConfig:
    return ExecutionPipelineConfig(
        input_source=_provider_config("input_source", pipeline, INPUT_PROVIDERS),
        executor=_provider_config(
            "executor",
            pipeline,
            EXECUTOR_PROVIDERS,
            "ORCHESTRATOR_EXECUTOR_EXECUTION",
        ),
        workspace_manager=_provider_config("workspace_manager", pipeline, WORKSPACE_PROVIDERS),
        destination=_provider_config("destination", pipeline, DESTINATION_PROVIDERS),
        labels=_stage_labels(pipeline, "execution"),
    )


def _review_pipeline_config(pipeline: dict[str, Any]) -> ReviewPipelineConfig:
    return ReviewPipelineConfig(
        input_source=_provider_config("input_source", pipeline, REVIEW_INPUT_PROVIDERS),
        executor=_provider_config(
            "executor",
            pipeline,
            REVIEW_EXECUTOR_PROVIDERS,
            "ORCHESTRATOR_EXECUTOR_REVIEW",
        ),
        workspace_manager=_provider_config("workspace_manager", pipeline, REVIEW_WORKSPACE_PROVIDERS),
        destination=_provider_config("destination", pipeline, REVIEW_DESTINATION_PROVIDERS),
        labels=_stage_labels(pipeline, "review"),
    )


def _triage_pipeline_config(pipeline: dict[str, Any]) -> TriagePipelineConfig:
    return TriagePipelineConfig(
        input_source=_provider_config("input_source", pipeline, TRIAGE_INPUT_PROVIDERS),
        executor=_provider_config("executor", pipeline, TRIAGE_EXECUTOR_PROVIDERS, "ORCHESTRATOR_EXECUTOR_TRIAGE"),
        destination=_provider_config("destination", pipeline, TRIAGE_DESTINATION_PROVIDERS),
        labels=_triage_labels(pipeline),
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
        triage=_triage_pipeline_config(pipeline.get("triage") or {}),
    )


def load_review_pipeline_config() -> ReviewPipelineConfig:
    """Return the review provider configuration from the main pipeline config."""
    return load_pipeline_config().review


# Both sections are cached together, so preserve a review-specific cache-clear
# hook without allowing a stale review configuration.
load_review_pipeline_config.cache_clear = load_pipeline_config.cache_clear  # type: ignore[attr-defined]


def load_triage_pipeline_config() -> TriagePipelineConfig:
    """Return the triage provider configuration from the main pipeline config."""
    return load_pipeline_config().triage


load_triage_pipeline_config.cache_clear = load_pipeline_config.cache_clear  # type: ignore[attr-defined]


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
def load_triage_model_config() -> ModelConfig | None:
    return _load_model_config("triage")


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
            legacy_label = entry.get("label")
            if legacy_label not in (None, EXECUTION_READY_LABEL):
                raise ValueError(
                    f"repository {name!r} has unsupported legacy label {legacy_label!r}; "
                    f"remove it or set label: {EXECUTION_READY_LABEL!r}. "
                    "Stage labels belong under pipeline.*.labels."
                )
            # ``label: ai-agent`` was the old repository filter.  Accept it
            # while intentionally dropping it so it cannot override the
            # execution stage contract.
            result[name] = {
                k: v for k, v in entry.items() if k not in {"name", "label"}
            }
    return result


def is_repository_allowed(repository: str) -> bool:
    return repository in load_repository_config()


def repository_command(repository: str) -> str:
    """Comment prefix that triggers a re-run for the repo (default /ai-agent)."""
    return load_repository_config().get(repository, {}).get("command") or "/ai-agent"


def allowed_repositories() -> list[str]:
    return sorted(load_repository_config())
