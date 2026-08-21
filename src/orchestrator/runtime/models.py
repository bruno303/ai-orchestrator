"""Typed provider-neutral requests and results for runtime operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.domain import Context, PublishedChange, PublishedReview, ReviewOutcome, ReviewTarget, WorkItem
from orchestrator.providers import ExecutionResult, ReviewRequest, WorkspaceResult


def _as_context(value: Context | dict[str, Any]) -> Context:
    if isinstance(value, Context):
        return value
    if value and all(isinstance(item, dict) for item in value.values()):
        return Context.from_dict(value)
    return Context({"legacy": value}) if value else Context()


@dataclass(frozen=True)
class WorkContext:
    item: WorkItem

    @property
    def task_id(self) -> str:
        return self.item.id

    @property
    def repository(self) -> str:
        return self.item.repository

    @property
    def title(self) -> str:
        return self.item.title

    @property
    def body(self) -> str:
        return self.item.description

    @property
    def extra_context(self) -> tuple[str, ...]:
        return self.item.extra_context


class IssueContext(WorkContext):
    """Deprecated constructor adapter for pre-domain callers."""

    def __init__(self, task_id: str, repository: str, legacy_number: int | None = None,
                 title: str = "", body: str = "", extra_context: list[str] | None = None,
                 provider_state: dict[str, Any] | None = None, work_item_id: str = "") -> None:
        legacy = dict(provider_state or {})
        data: dict[str, dict[str, Any]] = {}
        if legacy_number is not None:
            data["github"] = {"issue_number": legacy_number}
        if legacy:
            data["legacy"] = legacy
        super().__init__(WorkItem(
            work_item_id or task_id, repository, title, body, tuple(extra_context or ()),
            context=Context(data),
        ))


@dataclass(frozen=True)
class PrepareExecutionRequest:
    work: WorkContext
    branch: str = ""
    base_branch: str = ""
    workspace: str = ""
    context: Context = field(default_factory=Context)


@dataclass(frozen=True)
class PrepareExecutionResult:
    work: WorkContext
    workspace: WorkspaceResult
    base_branch: str
    context: Context = field(default_factory=Context)


@dataclass(frozen=True)
class AgentRequest:
    work: WorkContext
    node: str
    agent: str
    prompt: str
    workspace: str
    context: Context = field(default_factory=Context)


@dataclass(frozen=True)
class PhaseResult:
    execution: ExecutionResult
    attempts: int
    context: Context

    @property
    def provider_state(self) -> dict[str, Any]:
        data = self.context.to_dict()
        return dict(next(iter(data.values()))) if len(data) == 1 else data


@dataclass(frozen=True)
class PlanRequest:
    work: WorkContext
    workspace: str
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", _as_context(self.context))


@dataclass(frozen=True)
class PlanResult:
    summary: str
    plan_path: str
    phase: PhaseResult


@dataclass(frozen=True)
class ImplementationRequest:
    work: WorkContext
    workspace: str
    plan_path: str = ".agents/plans/plan.md"
    context: Context = field(default_factory=Context)
    provider_state: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        value = self.context if self.context else self.provider_state
        object.__setattr__(self, "context", _as_context(value))


@dataclass(frozen=True)
class ImplementationResult:
    summary: str
    phase: PhaseResult


@dataclass(frozen=True)
class TestRequest:
    work: WorkContext
    workspace: str
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", _as_context(self.context))


@dataclass(frozen=True)
class TestResult:
    summary: str
    phase: PhaseResult


@dataclass(frozen=True)
class PublishRequest:
    work: WorkContext
    workspace: str
    source_ref: str
    target_ref: str
    context: Context = field(default_factory=Context)


@dataclass(frozen=True)
class PublishResult:
    publication: PublishedChange


@dataclass(frozen=True)
class CleanupRequest:
    repository: str
    workspace: WorkspaceResult


@dataclass(frozen=True)
class CleanupResult:
    workspace: str


@dataclass(frozen=True)
class PrepareReviewRequest:
    target: ReviewTarget
    workspace: str = ""


@dataclass(frozen=True)
class PrepareReviewResult:
    target: ReviewTarget
    workspace: WorkspaceResult
    context: Context


@dataclass(frozen=True)
class ExecuteReviewRequest:
    prepared: PrepareReviewResult
    prompt: str


@dataclass(frozen=True)
class ExecuteReviewResult:
    request: ReviewRequest
    outcome: ReviewOutcome


@dataclass(frozen=True)
class PublishReviewRequest:
    prepared: PrepareReviewResult
    execution: ExecuteReviewResult


@dataclass(frozen=True)
class PublishReviewResult:
    publication: PublishedReview


@dataclass(frozen=True)
class CleanupReviewRequest:
    prepared: PrepareReviewResult


@dataclass(frozen=True)
class CleanupReviewResult:
    workspace: str
