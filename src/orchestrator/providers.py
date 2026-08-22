"""Provider-neutral contracts used at the runtime integration boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Callable, Protocol, runtime_checkable

from orchestrator.domain import (
    ChangeRequest,
    Context,
    PublishedChange,
    PublishedReview,
    ReviewOutcome,
    ReviewTarget,
    WorkItem,
)


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
    """Return the plain, checkpoint- and JSON-serializable form of a model."""
    result = _json_value(asdict(value))  # type: ignore[arg-type]
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"{type(value).__name__} contains values that are not JSON-serializable"
        ) from exc
    return result


class ExecutorError(RuntimeError):
    """Adapter-neutral execution failure.

    ``retryable`` lets an executor classify failures without exposing its own
    exception types to the workflow runtime.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class InputEvent:
    """Lifecycle event around the canonical execution work item."""

    event_id: str
    work_item: WorkItem
    trigger: str = "new"
    context: Context = field(default_factory=Context)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ExecutionRequest:
    task_id: str
    workspace: str
    prompt: str
    agent: str
    model: str | None = None
    variant: str | None = None
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ExecutionResult:
    success: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class WorkspaceRequest:
    task_id: str
    repository: str
    branch: str
    base_branch: str
    purpose: str = "execution"
    repository_url: str = ""
    fetch_url: str = ""
    target_ref: str = ""
    revision: str = ""
    checkout_mode: str = "branch"
    workspace: str = ""
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class WorkspaceResult:
    workspace: str
    branch: str
    context: Context = field(default_factory=Context)
    base_branch: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ReviewRequest:
    task_id: str
    repository: str
    workspace: str
    prompt: str
    context: Context = field(default_factory=Context)
    log_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@runtime_checkable
class InputSource(Protocol):
    def poll(self) -> list[InputEvent]: ...


@runtime_checkable
class SourceFeedback(Protocol):
    """Semantic lifecycle notifications for an input event."""

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


class UnknownProviderError(ValueError):
    """Raised when configuration names a provider not registered for a boundary."""


ProviderFactory = Callable[[dict[str, Any]], object]


class ProviderRegistry:
    """Registry of provider names, kept separate by runtime boundary."""

    def __init__(self, providers: dict[str, ProviderFactory], boundary: type | None = None) -> None:
        self._providers = dict(providers)
        self._boundary = boundary

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def get(self, provider_type: str) -> ProviderFactory:
        try:
            return self._providers[provider_type]
        except KeyError as exc:
            raise UnknownProviderError(f"unknown provider type: {provider_type}") from exc

    def create(self, provider_type: str, options: dict[str, Any] | None = None) -> object:
        provider = self.get(provider_type)(dict(options or {}))
        if self._boundary is not None and not isinstance(provider, self._boundary):
            raise TypeError(f"provider factory {provider_type} returned an invalid implementation")
        return provider


@dataclass
class _PlaceholderInputSource:
    options: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> list[InputEvent]:
        return []


@dataclass
class _PlaceholderExecutor:
    options: dict[str, Any] = field(default_factory=dict)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            exit_code=1,
            stderr="executor provider is not wired",
            context=request.context.merge_namespace("placeholder", {"options": self.options}),
        )


@dataclass
class _PlaceholderWorkspaceManager:
    options: dict[str, Any] = field(default_factory=dict)

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        return WorkspaceResult(
            workspace="",
            branch=request.branch,
            context=request.context.merge_namespace("placeholder", {"options": self.options}),
        )

    def cleanup(self, result: WorkspaceResult) -> None:
        return None


@dataclass
class _PlaceholderDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, request: ChangeRequest) -> PublishedChange:
        return PublishedChange(provider="placeholder", context=request.context)


def _input_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderInputSource(options)
    from orchestrator.github_input import GitHubPollingInputSource, GitHubSourceFeedback

    source = GitHubPollingInputSource(options=options)
    source.feedback = GitHubSourceFeedback(source.github_client)
    return source


def _executor_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderExecutor(options)
    from orchestrator.opencode import OpenCodeExecutor

    return OpenCodeExecutor(options=options)


def _workspace_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderWorkspaceManager(options)
    from orchestrator.git_workspace import GitWorkspaceManager

    return GitWorkspaceManager(options=options)


def _destination_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderDestination(options)
    from orchestrator.github_destination import GitHubDestination

    return GitHubDestination(options=options)


INPUT_PROVIDERS = ProviderRegistry({"github_polling": _input_factory}, InputSource)
EXECUTOR_PROVIDERS = ProviderRegistry({"opencode": _executor_factory}, Executor)
WORKSPACE_PROVIDERS = ProviderRegistry({"git": _workspace_factory}, WorkspaceManager)
DESTINATION_PROVIDERS = ProviderRegistry({"github": _destination_factory}, Destination)


@dataclass
class _PlaceholderReviewInputSource:
    options: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> list[ReviewTarget]:
        return []


@dataclass
class _PlaceholderReviewExecutor:
    options: dict[str, Any] = field(default_factory=dict)

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        return ReviewOutcome(False, summary="review executor provider is not wired", context=request.context)


@dataclass
class _PlaceholderReviewDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, target: ReviewTarget, outcome: ReviewOutcome) -> PublishedReview:
        return PublishedReview(provider="placeholder", context=target.context)


def _review_input_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderReviewInputSource(options)
    from orchestrator.github_review import GitHubReviewInputSource
    options.pop("store", None)
    return GitHubReviewInputSource(options=options)


def _review_executor_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderReviewExecutor(options)
    from orchestrator.opencode import OpenCodeReviewExecutor
    return OpenCodeReviewExecutor(options=options)


def _review_destination_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderReviewDestination(options)
    from orchestrator.github_review import GitHubReviewDestination
    return GitHubReviewDestination(options=options)


REVIEW_INPUT_PROVIDERS = ProviderRegistry({"github_polling": _review_input_factory}, ReviewInputSource)
REVIEW_EXECUTOR_PROVIDERS = ProviderRegistry({"opencode": _review_executor_factory}, ReviewExecutor)
REVIEW_WORKSPACE_PROVIDERS = ProviderRegistry({"git": _workspace_factory}, WorkspaceManager)
REVIEW_DESTINATION_PROVIDERS = ProviderRegistry({"github": _review_destination_factory}, ReviewDestination)
