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

from orchestrator.application.workflow import (
    COMMENT_IMPL_STEP_TYPES,
    DISCUSS_STEP_TYPES,
    EXECUTION_STEP_TYPES,
    REVIEW_STEP_TYPES,
    TRIAGE_STEP_TYPES,
    StepSpec,
    default_steps,
    validate_steps,
)
from orchestrator.main.providers import (
    DESTINATION_PROVIDERS,
    DISCUSSION_EXECUTOR_PROVIDERS,
    EXECUTOR_PROVIDERS,
    INPUT_PROVIDERS,
    ProviderRegistry,
    WORKSPACE_PROVIDERS,
    REVIEW_DESTINATION_PROVIDERS,
    REVIEW_EXECUTOR_PROVIDERS,
    REVIEW_INPUT_PROVIDERS,
    REVIEW_WORKSPACE_PROVIDERS,
    TRIAGE_DESTINATION_PROVIDERS,
    TRIAGE_EXECUTOR_PROVIDERS,
    TRIAGE_INPUT_PROVIDERS,
    UnknownProviderError,
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
# Default wall-clock budget for one ``command`` workflow step.
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("ORCHESTRATOR_COMMAND_TIMEOUT", "3600"))
MAX_CONCURRENT_TASKS = int(os.environ.get("ORCHESTRATOR_MAX_CONCURRENT", "1"))
# A task/comment with no activity for this long is considered dead (process died).
STALE_SECONDS = int(os.environ.get("ORCHESTRATOR_STALE_SECONDS", str(2 * 60 * 60)))


@dataclass
class ModelConfig:
    """Model selection for agent runs."""

    name: str | None
    variant: str | None


@dataclass(frozen=True)
class LoggingConfig:
    """Application-wide logging verbosity."""

    verbosity: str = "normal"


LOGGING_VERBOSITIES = {"quiet", "normal", "verbose"}


@lru_cache(maxsize=1)
def load_logging_config() -> LoggingConfig:
    """Load and validate the global logging configuration."""
    if not CONFIG_FILE.exists():
        data = {}
    else:
        with CONFIG_FILE.open() as fh:
            data = yaml.safe_load(fh) or {}
    raw = data.get("logging") or {}
    if not isinstance(raw, dict):
        raise ValueError("logging must be a mapping with verbosity quiet, normal, or verbose")
    verbosity = raw.get("verbosity", "normal")
    if verbosity not in LOGGING_VERBOSITIES:
        raise ValueError(
            f"logging.verbosity must be one of quiet, normal, verbose; got {verbosity!r}"
        )
    return LoggingConfig(verbosity=verbosity)


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
    workflow: tuple[StepSpec, ...] = field(default_factory=lambda: default_steps("execution"))
    comment_impl_workflow: tuple[StepSpec, ...] = field(
        default_factory=lambda: default_steps("comment_impl")
    )
    comment_discuss_workflow: tuple[StepSpec, ...] = field(
        default_factory=lambda: default_steps("discuss")
    )


@dataclass(frozen=True)
class ReviewPipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    workspace_manager: ProviderConfig
    destination: ProviderConfig
    labels: "StageLabelConfig" = field(default_factory=lambda: _default_stage_labels("review"))
    workflow: tuple[StepSpec, ...] = field(default_factory=lambda: default_steps("review"))


@dataclass(frozen=True)
class TriagePipelineConfig:
    input_source: ProviderConfig
    executor: ProviderConfig
    destination: ProviderConfig
    labels: "TriageLabelConfig" = field(default_factory=lambda: _default_triage_labels())
    workflow: tuple[StepSpec, ...] = field(default_factory=lambda: default_steps("triage"))


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


_PIPELINE_SECTION_KEYS = ("execution", "review", "triage")
_EXECUTION_SECTION_KEYS = (
    "input_source",
    "executor",
    "workspace_manager",
    "destination",
    "labels",
    "workflow",
    "comments",
)
_REVIEW_SECTION_KEYS = (
    "input_source",
    "executor",
    "workspace_manager",
    "destination",
    "labels",
    "workflow",
)
_TRIAGE_SECTION_KEYS = ("input_source", "executor", "destination", "labels", "workflow")
_COMMENT_SECTION_KEYS = ("impl", "discuss")
_COMMENT_WORKFLOW_KEYS = ("workflow",)
_WORKFLOW_KEYS = ("steps", "require_publication")


def _validated_mapping(value: Any, *, path: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    """Reject unknown keys in one workflow-stage config section.

    Sections are fail-fast: an unknown key is a typo that would otherwise be
    silently ignored.  Provider option mappings and ``labels`` internals are
    deliberately free-form and never routed through this helper.
    """
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping; got {type(value).__name__}")
    unknown = sorted({str(key) for key in value} - set(allowed))
    if unknown:
        raise ValueError(
            f"{path}: unknown key(s) {', '.join(repr(key) for key in unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    return value


_STEP_ENTRY_KEYS = ("name", "type")
_STEP_OPTIONAL_KEYS = (
    "on_failure",
    "executor",
    "model",
    "prompt_file",
    "command",
    "timeout_seconds",
    "agent",
)


def _step_entry(entry: Any, *, index: int, path: str, registry: ProviderRegistry) -> StepSpec:
    """Parse one workflow step entry, anchored to its config path."""
    label = f"{path}.steps[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{label} must be a step mapping; got {type(entry).__name__}")
    name = entry.get("name")
    if isinstance(name, str) and name:
        label = f"{label} '{name}'"
    unknown = sorted(str(key) for key in set(entry) - {*_STEP_ENTRY_KEYS, *_STEP_OPTIONAL_KEYS})
    if unknown:
        raise ValueError(
            f"{label}: unknown step key(s) {', '.join(unknown)} "
            f"(allowed: {', '.join((*_STEP_ENTRY_KEYS, *_STEP_OPTIONAL_KEYS))})"
        )
    for key in _STEP_ENTRY_KEYS:
        if key not in entry:
            raise ValueError(f"{label}: missing required step key '{key}'")
        value = entry[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}: step '{key}' must be a non-empty string, got {value!r}")
    on_failure = entry.get("on_failure")
    if on_failure is None:
        on_failure = "abort"
    executor_type = _step_executor(
        entry.get("executor"), label=label, registry=registry,
    )
    model_name, model_variant = _step_model(entry.get("model"), label=label)
    for key in ("prompt_file", "command", "agent"):
        value = entry.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(
                f"{label}: '{key}' must be a non-empty string when present, got {value!r}"
            )
    timeout_seconds = entry.get("timeout_seconds")
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError(
            f"{label}: 'timeout_seconds' must be a positive integer, got {timeout_seconds!r}"
        )
    return StepSpec(
        name=entry["name"],
        type=entry["type"],
        on_failure=on_failure,
        executor_type=executor_type,
        model_name=model_name,
        model_variant=model_variant,
        prompt_file=entry.get("prompt_file"),
        command=entry.get("command"),
        timeout_seconds=timeout_seconds,
        agent=entry.get("agent"),
    )


def _step_executor(value: Any, *, label: str, registry: ProviderRegistry) -> str | None:
    """Validate one ``executor`` override against the stage's executor registry."""
    if value is None:
        return None
    if isinstance(value, str):
        executor_type = value
    elif isinstance(value, dict):
        unknown = sorted(str(key) for key in set(value) - {"type"})
        if unknown:
            raise ValueError(
                f"{label}: 'executor' accepts only a 'type' key; got {', '.join(unknown)}"
            )
        executor_type = value.get("type")
        if not isinstance(executor_type, str) or not executor_type.strip():
            raise ValueError(f"{label}: 'executor' requires a non-empty 'type' string")
    else:
        raise ValueError(
            f"{label}: 'executor' must be a provider type string or a '{{type: ...}}' "
            f"mapping; got {type(value).__name__}"
        )
    try:
        registry.get(executor_type)
    except UnknownProviderError as exc:
        raise ValueError(f"{label}: unknown executor provider type: {executor_type}") from exc
    return executor_type


def _step_model(value: Any, *, label: str) -> tuple[str | None, str | None]:
    """Validate one ``model`` override mapping."""
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError(f"{label}: 'model' must be a mapping with optional 'name' and 'variant'")
    unknown = sorted(str(key) for key in set(value) - {"name", "variant"})
    if unknown:
        raise ValueError(f"{label}: 'model' accepts only 'name' and 'variant'; got {', '.join(unknown)}")
    fields: list[tuple[str, Any]] = [
        ("name", value.get("name")),
        ("variant", value.get("variant")),
    ]
    for key, field_value in fields:
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(
                f"{label}: 'model.{key}' must be a string, got {type(field_value).__name__}"
            )
    return value.get("name"), value.get("variant")


def _workflow_steps(
    value: Any,
    *,
    stage: str,
    path: str,
    allowed_types: tuple[str, ...],
    registry: ProviderRegistry,
) -> tuple[StepSpec, ...]:
    """Parse one optional ``workflow`` config section into validated steps.

    An absent section (or ``null``) keeps the built-in step sequence.  A
    present mapping must carry a ``steps`` list; a bare step list is accepted
    as shorthand.  The raw YAML becomes :class:`StepSpec` entries revalidated
    by :func:`validate_steps` against the stage catalog.
    """
    if value is None:
        return default_steps(stage)
    require_publication = True
    if isinstance(value, dict):
        workflow = _validated_mapping(value, path=path, allowed=_WORKFLOW_KEYS)
        # Validation-only flag: deliberately not carried on StepSpec or any
        # pipeline dataclass.
        require_publication = workflow.get("require_publication", True)
        if not isinstance(require_publication, bool):
            raise ValueError(
                f"{path}.require_publication must be a boolean; "
                f"got {require_publication!r}"
            )
        if "steps" not in workflow:
            raise ValueError(f"{path}: missing required 'steps' list")
        raw_steps = workflow["steps"]
    elif isinstance(value, list):
        raw_steps = value
    else:
        raise ValueError(
            f"{path} must be a mapping with a 'steps' list or a bare step list; "
            f"got {type(value).__name__}"
        )
    if not isinstance(raw_steps, list):
        raise ValueError(f"{path}.steps must be a list of step mappings; got {type(raw_steps).__name__}")
    if not raw_steps:
        raise ValueError(f"{path}.steps must contain at least one step")
    steps: list[StepSpec] = []
    for index, entry in enumerate(raw_steps):
        steps.append(_step_entry(entry, index=index, path=path, registry=registry))
    validate_steps(steps, allowed_types, stage=stage)
    if require_publication and not any(step.type == "publish" for step in steps):
        raise ValueError(
            f"{path}: workflow has no 'publish' step; add one or set "
            "require_publication: false"
        )
    return tuple(steps)


def _comment_workflow_steps(
    comments: dict[str, Any], *, section: str, stage: str, allowed_types: tuple[str, ...],
    registry: ProviderRegistry,
) -> tuple[StepSpec, ...]:
    """Parse one ``pipeline.execution.comments.<section>.workflow`` section."""
    raw = _validated_mapping(
        comments.get(section) or {},
        path=f"pipeline.execution.comments.{section}",
        allowed=_COMMENT_WORKFLOW_KEYS,
    )
    return _workflow_steps(
        raw.get("workflow"),
        stage=stage,
        path=f"pipeline.execution.comments.{section}.workflow",
        allowed_types=allowed_types,
        registry=registry,
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
    pipeline = _validated_mapping(
        pipeline, path="pipeline.execution", allowed=_EXECUTION_SECTION_KEYS,
    )
    comments = _validated_mapping(
        pipeline.get("comments") or {},
        path="pipeline.execution.comments",
        allowed=_COMMENT_SECTION_KEYS,
    )
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
        workflow=_workflow_steps(
            pipeline.get("workflow"),
            stage="execution",
            path="pipeline.execution.workflow",
            allowed_types=EXECUTION_STEP_TYPES,
            registry=EXECUTOR_PROVIDERS,
        ),
        comment_impl_workflow=_comment_workflow_steps(
            comments, section="impl", stage="comment_impl",
            allowed_types=COMMENT_IMPL_STEP_TYPES, registry=EXECUTOR_PROVIDERS,
        ),
        comment_discuss_workflow=_comment_workflow_steps(
            comments, section="discuss", stage="discuss",
            allowed_types=DISCUSS_STEP_TYPES, registry=DISCUSSION_EXECUTOR_PROVIDERS,
        ),
    )


def _review_pipeline_config(pipeline: dict[str, Any]) -> ReviewPipelineConfig:
    pipeline = _validated_mapping(
        pipeline, path="pipeline.review", allowed=_REVIEW_SECTION_KEYS,
    )
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
        workflow=_workflow_steps(
            pipeline.get("workflow"),
            stage="review",
            path="pipeline.review.workflow",
            allowed_types=REVIEW_STEP_TYPES,
            registry=REVIEW_EXECUTOR_PROVIDERS,
        ),
    )


def _triage_pipeline_config(pipeline: dict[str, Any]) -> TriagePipelineConfig:
    pipeline = _validated_mapping(
        pipeline, path="pipeline.triage", allowed=_TRIAGE_SECTION_KEYS,
    )
    return TriagePipelineConfig(
        input_source=_provider_config("input_source", pipeline, TRIAGE_INPUT_PROVIDERS),
        executor=_provider_config("executor", pipeline, TRIAGE_EXECUTOR_PROVIDERS, "ORCHESTRATOR_EXECUTOR_TRIAGE"),
        destination=_provider_config("destination", pipeline, TRIAGE_DESTINATION_PROVIDERS),
        labels=_triage_labels(pipeline),
        workflow=_workflow_steps(
            pipeline.get("workflow"),
            stage="triage",
            path="pipeline.triage.workflow",
            allowed_types=TRIAGE_STEP_TYPES,
            registry=TRIAGE_EXECUTOR_PROVIDERS,
        ),
    )


@lru_cache(maxsize=1)
def load_pipeline_config() -> PipelineConfig:
    """Load provider selections from the optional ``pipeline`` config section."""
    if not CONFIG_FILE.exists():
        data = {}
    else:
        with CONFIG_FILE.open() as fh:
            data = yaml.safe_load(fh) or {}
    pipeline = _validated_mapping(
        data.get("pipeline") or {}, path="pipeline", allowed=_PIPELINE_SECTION_KEYS,
    )
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


def allowed_repositories() -> list[str]:
    return sorted(load_repository_config())
