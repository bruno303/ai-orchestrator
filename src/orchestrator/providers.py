"""Provider-neutral contracts used at the runtime integration boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Callable, Protocol, runtime_checkable


def _json_dict(value: object) -> dict[str, Any]:
    """Return the plain, checkpoint- and JSON-serializable form of a model."""
    result = asdict(value)  # type: ignore[arg-type]
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


@dataclass
class InputEvent:
    event_id: str
    repository: str
    title: str
    body: str = ""
    number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provider_state: dict[str, Any] = field(default_factory=dict)
    provider: str = ""

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
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ExecutionResult:
    success: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class WorkspaceRequest:
    task_id: str
    repository: str
    branch: str
    base_branch: str
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class WorkspaceResult:
    workspace: str
    branch: str
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class Artifact:
    path: str
    kind: str = "file"
    metadata: dict[str, Any] = field(default_factory=dict)

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
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class PublicationResult:
    number: int | None = None
    url: str | None = None
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ReviewEvent:
    """Provider-neutral notification that a pull request should be reviewed."""

    event_id: str
    repository: str
    title: str = ""
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    provider_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(self)


@dataclass
class ReviewRequest:
    task_id: str
    repository: str
    workspace: str
    prompt: str
    provider_state: dict[str, Any] = field(default_factory=dict)

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
    def poll(self) -> list[ReviewEvent]: ...


@runtime_checkable
class ReviewExecutor(Protocol):
    def execute(self, request: ReviewRequest) -> ReviewResult: ...


@runtime_checkable
class ReviewDestination(Protocol):
    def publish(self, request: ReviewRequest, result: ReviewResult) -> None: ...


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

    def publish(self, request: PublicationRequest) -> PublicationResult:
        return PublicationResult(provider_state={"options": self.options})


def _input_factory(options: dict[str, Any]) -> object:
    if not options.pop("_runtime", False):
        return _PlaceholderInputSource(options)
    from orchestrator.github_input import GitHubPollingInputSource
    from orchestrator.persistence import TaskStore

    return GitHubPollingInputSource(options.pop("store", None) or TaskStore(), options=options)


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

    def poll(self) -> list[ReviewEvent]:
        return []


@dataclass
class _PlaceholderReviewExecutor:
    options: dict[str, Any] = field(default_factory=dict)

    def execute(self, request: ReviewRequest) -> ReviewResult:
        return ReviewResult(False, summary="review executor provider is not wired", provider_state={"options": self.options})


@dataclass
class _PlaceholderReviewDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, request: ReviewRequest, result: ReviewResult) -> None:
        return None


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
