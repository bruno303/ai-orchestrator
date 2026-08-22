"""Provider-neutral application ports and integration data."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Protocol, runtime_checkable

from orchestrator.domain import ChangeRequest, Context, PublishedChange, PublishedReview, ReviewOutcome, ReviewTarget, WorkItem


def _json_value(value: Any) -> Any:
    if isinstance(value, Context):
        return value.to_dict()
    if hasattr(value, "to_dict") and not hasattr(value, "__dataclass_fields__"):
        return value.to_dict()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _json_dict(value: object) -> dict[str, Any]:
    result = _json_value(asdict(value))  # type: ignore[arg-type]
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{type(value).__name__} contains values that are not JSON-serializable") from exc
    return result


class ExecutorError(RuntimeError):
    """Adapter-neutral execution failure."""


@dataclass(frozen=True)
class InputEvent:
    event_id: str
    work_item: WorkItem
    trigger: str = "new"
    context: Context = field(default_factory=Context)
    metadata: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]: return _json_dict(self)


@dataclass
class ExecutionRequest:
    task_id: str; workspace: str; prompt: str; agent: str
    model: str | None = None; variant: str | None = None; context: Context = field(default_factory=Context)
    def to_dict(self) -> dict[str, Any]: return _json_dict(self)


@dataclass
class ExecutionResult:
    success: bool; exit_code: int; stdout: str = ""; stderr: str = ""; duration_seconds: float = 0.0
    context: Context = field(default_factory=Context)
    def to_dict(self) -> dict[str, Any]: return _json_dict(self)


@dataclass
class WorkspaceRequest:
    task_id: str; repository: str; branch: str; base_branch: str; purpose: str = "execution"
    repository_url: str = ""; fetch_url: str = ""; target_ref: str = ""; revision: str = ""; checkout_mode: str = "branch"; workspace: str = ""
    context: Context = field(default_factory=Context)
    def to_dict(self) -> dict[str, Any]: return _json_dict(self)


@dataclass
class WorkspaceResult:
    workspace: str; branch: str; context: Context = field(default_factory=Context); base_branch: str = ""
    def to_dict(self) -> dict[str, Any]: return _json_dict(self)


@dataclass
class ReviewRequest:
    task_id: str; repository: str; workspace: str; prompt: str; context: Context = field(default_factory=Context); log_file: str = ""
    def to_dict(self) -> dict[str, Any]: return _json_dict(self)


class ContextPresenter(Protocol):
    def logging_fields(self, context: Context) -> dict[str, Any]: ...


class NoopContextPresenter:
    def logging_fields(self, context: Context) -> dict[str, Any]: return {}


@runtime_checkable
class InputSource(Protocol):
    def poll(self) -> list[InputEvent]: ...
@runtime_checkable
class SourceFeedback(Protocol):
    def mark_started(self, event: InputEvent) -> None: ...
    def mark_succeeded(self, event: InputEvent) -> None: ...
    def mark_failed(self, event: InputEvent, error: str | None = None) -> None: ...
@runtime_checkable
class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
@runtime_checkable
class WorkspaceManager(Protocol):
    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult: ...
    def cleanup(self, result: WorkspaceResult) -> None: ...
@runtime_checkable
class Destination(Protocol):
    def publish(self, request: ChangeRequest) -> PublishedChange: ...
@runtime_checkable
class ReviewInputSource(Protocol):
    def poll(self) -> list[ReviewTarget]: ...
@runtime_checkable
class ReviewExecutor(Protocol):
    def execute(self, request: ReviewRequest) -> ReviewOutcome: ...
@runtime_checkable
class ReviewDestination(Protocol):
    def publish(self, target: ReviewTarget, outcome: ReviewOutcome) -> PublishedReview: ...
