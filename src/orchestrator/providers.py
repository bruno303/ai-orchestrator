"""Provider-neutral contracts used at the runtime integration boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from orchestrator.domain import (
    Artifact,
    ChangeRequest,
    Context,
    ContextPresenter,
    PublishedChange,
    PublishedReview,
    ReviewOutcome,
    ReviewTarget,
    WorkItem,
    ensure_task_id,
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


def validate_provider_state(value: dict[str, Any]) -> dict[str, Any]:
    """Validate provider-owned state before it is admitted to a checkpoint."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError("provider_state contains values that are not JSON-serializable") from exc
    return value


ProviderContext = Context


def _legacy_context(value: Context | Mapping[str, Any] | None, namespace: str) -> Context:
    if isinstance(value, Context):
        return value
    data = dict(value or {})
    if not data:
        return Context()
    if all(isinstance(item, Mapping) for item in data.values()):
        return Context(data)  # type: ignore[arg-type]
    return Context({namespace: data})


class ExecutorError(RuntimeError):
    """Adapter-neutral execution failure.

    ``retryable`` lets an executor classify failures without exposing its own
    exception types to the workflow runtime.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, init=False)
class InputEvent:
    """Lifecycle event around the canonical execution work item."""

    event_id: str
    work_item: WorkItem
    trigger: str
    context: Context
    metadata: dict[str, Any]

    def __init__(
        self,
        event_id: str,
        work_item: WorkItem | str,
        title: str = "",
        body: str = "",
        number: int | None = None,
        metadata: dict[str, Any] | None = None,
        provider_state: Context | Mapping[str, Any] | None = None,
        provider: str = "",
        extra_context: list[str] | tuple[str, ...] | None = None,
        work_item_id: str = "",
        trigger: str = "new",
        context: Context | Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(work_item, WorkItem):
            item = work_item
        else:
            legacy = _legacy_context(provider_state, provider or "legacy")
            source_id = work_item_id or (
                f"{work_item}#{number}" if number is not None else ensure_task_id(event_id)
            )
            item = WorkItem(
                source_id, work_item, title, body, tuple(extra_context or ()), provider, legacy
            )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "work_item", item)
        object.__setattr__(self, "trigger", trigger)
        object.__setattr__(self, "context", _legacy_context(context, item.input_provider or "event"))
        object.__setattr__(self, "metadata", dict(metadata or {}))

    @property
    def repository(self) -> str:
        return self.work_item.repository

    @property
    def title(self) -> str:
        return self.work_item.title

    @property
    def body(self) -> str:
        return self.work_item.description

    @property
    def extra_context(self) -> list[str]:
        return list(self.work_item.extra_context)

    @property
    def provider(self) -> str:
        return self.work_item.input_provider

    @property
    def work_item_id(self) -> str:
        return self.work_item.id

    @property
    def provider_state(self) -> dict[str, Any]:
        """Deprecated flattened compatibility view."""
        combined = self.work_item.context.merged(self.context).to_dict()
        if len(combined) == 1:
            return dict(next(iter(combined.values())))
        return combined

    @property
    def number(self) -> int | None:
        value = self.work_item.context.namespace("github").get("issue_number")
        if value is None:
            value = self.provider_state.get("source_number")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

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
    provider_state: dict[str, Any] = field(default_factory=ProviderContext)
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
    provider_state: dict[str, Any] = field(default_factory=ProviderContext)
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class WorkspaceRequest:
    task_id: str
    repository: str
    branch: str
    base_branch: str
    provider_state: dict[str, Any] = field(default_factory=ProviderContext)
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
    provider_state: dict[str, Any] = field(default_factory=ProviderContext)
    context: Context = field(default_factory=Context)
    base_branch: str = ""

    def __post_init__(self) -> None:
        if not self.base_branch and isinstance(self.provider_state, dict):
            self.base_branch = str(self.provider_state.get("base_branch", ""))

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class PublicationRequest:
    repository: str
    title: str
    body: str
    head: str
    base: str
    artifacts: list[Artifact] = field(default_factory=list)
    provider_state: dict[str, Any] = field(default_factory=ProviderContext)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class PublicationResult:
    number: int | None = None
    url: str | None = None
    provider_state: dict[str, Any] = field(default_factory=dict)

    @property
    def external_id(self) -> str | None:
        """Generic external reference; ``number`` remains checkpoint compatible."""
        return str(self.number) if self.number is not None else None

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass(frozen=True, init=False)
class ReviewEvent(ReviewTarget):
    """Deprecated constructor-compatible alias for :class:`ReviewTarget`."""

    metadata: dict[str, Any]

    def __init__(self, event_id: str, repository: str, title: str = "", body: str = "",
                 metadata: dict[str, Any] | None = None,
                 provider_state: Context | Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        legacy = dict(provider_state or {}) if not isinstance(provider_state, Context) else {}
        context = kwargs.pop("context", None)
        if context is None and legacy:
            context = Context({
                "github": {
                    **({"pr_number": legacy["number"]} if legacy.get("number") is not None else {}),
                    **{key: legacy[key] for key in ("changed_files", "changed_lines", "author_login") if key in legacy},
                },
                "git": {
                    **{key: legacy[key] for key in ("repository_url", "fetch_url", "workspace") if key in legacy},
                    **({"revision": legacy["head_sha"]} if legacy.get("head_sha") else {}),
                },
            })
        context = context or _legacy_context(provider_state, "legacy")
        ReviewTarget.__init__(self, event_id, repository, title, body,
                              kwargs.pop("source_ref", legacy.get("head_ref", "")),
                              kwargs.pop("target_ref", legacy.get("base_ref", "")),
                              kwargs.pop("revision", legacy.get("head_sha", "")),
                              kwargs.pop("input_provider", ""), context)
        object.__setattr__(self, "metadata", dict(metadata or {}))

    @property
    def event_id(self) -> str:
        return self.id

    @property
    def body(self) -> str:
        return self.description

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ReviewRequest:
    task_id: str
    repository: str
    workspace: str
    prompt: str
    provider_state: dict[str, Any] = field(default_factory=dict)
    context: Context = field(default_factory=Context)
    log_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ReviewResult:
    success: bool
    verdict: str = ""
    summary: str = ""
    comments: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    provider_state: dict[str, Any] = field(default_factory=dict)

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
    def publish(self, request: PublicationRequest) -> PublicationResult: ...


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
            provider_state={"options": self.options},
        )


@dataclass
class _PlaceholderWorkspaceManager:
    options: dict[str, Any] = field(default_factory=dict)

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        return WorkspaceResult(
            workspace="",
            branch=request.branch,
            provider_state={"options": self.options},
        )

    def cleanup(self, result: WorkspaceResult) -> None:
        return None


@dataclass
class _PlaceholderDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, request: ChangeRequest) -> PublishedChange:
        if isinstance(request, PublicationRequest):
            return PublicationResult(provider_state={"options": self.options})
        return PublishedChange(provider="placeholder", context=request.context)


def _input_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderInputSource(options)
    from orchestrator.github_input import GitHubPollingInputSource, GitHubSourceFeedback
    from orchestrator.persistence import TaskStore

    store = options.pop("store", None) or TaskStore()
    source = GitHubPollingInputSource(store, options=options)
    source.feedback = GitHubSourceFeedback(store, source.github_client)
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
