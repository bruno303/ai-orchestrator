"""Typed provider-neutral requests and results for runtime operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from orchestrator.domain import Context, PublishedChange, PublishedReview, ReviewOutcome, ReviewTarget, WorkItem
from orchestrator.application.ports import ExecutionResult, ReviewRequest, WorkspaceResult


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
    context: Context

@dataclass(frozen=True)
class PlanRequest:
    work: WorkContext
    workspace: str
    context: Context = field(default_factory=Context)

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
@dataclass(frozen=True)
class ImplementationResult:
    summary: str
    phase: PhaseResult


@dataclass(frozen=True)
class TestRequest:
    work: WorkContext
    workspace: str
    context: Context = field(default_factory=Context)

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
