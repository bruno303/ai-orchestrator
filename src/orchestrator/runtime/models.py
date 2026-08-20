"""Typed requests and results for workflow runtime operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.providers import (
    ExecutionResult,
    PublicationResult,
    ReviewEvent,
    ReviewRequest,
    ReviewResult,
    WorkspaceResult,
)


@dataclass(frozen=True)
class IssueContext:
    """Provider-neutral work-item data needed by execution phases."""

    task_id: str
    repository: str
    # Retained as a compatibility alias for callers using the old positional
    # constructor. Generic runtime code uses work_item_id instead.
    issue_number: int | None = None
    title: str = ""
    body: str = ""
    extra_context: list[str] = field(default_factory=list)
    provider_state: dict[str, Any] = field(default_factory=dict)
    work_item_id: str = ""

    def __post_init__(self) -> None:
        if not self.work_item_id:
            object.__setattr__(self, "work_item_id", str(self.issue_number or self.task_id))


@dataclass(frozen=True)
class PrepareExecutionRequest:
    context: IssueContext
    branch: str = ""
    base_branch: str = ""
    workspace: str = ""
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PrepareExecutionResult:
    context: IssueContext
    workspace: WorkspaceResult
    base_branch: str


@dataclass(frozen=True)
class AgentRequest:
    context: IssueContext
    node: str
    agent: str
    prompt: str
    workspace: str
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhaseResult:
    execution: ExecutionResult
    attempts: int
    provider_state: dict[str, Any]


@dataclass(frozen=True)
class PlanRequest:
    context: IssueContext
    workspace: str
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanResult:
    summary: str
    plan_path: str
    phase: PhaseResult


@dataclass(frozen=True)
class ImplementationRequest:
    context: IssueContext
    workspace: str
    plan_path: str = ".agents/plans/plan.md"
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImplementationResult:
    summary: str
    phase: PhaseResult


@dataclass(frozen=True)
class TestRequest:
    context: IssueContext
    workspace: str
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TestResult:
    summary: str
    phase: PhaseResult


@dataclass(frozen=True)
class PublishRequest:
    context: IssueContext
    workspace: str
    head: str
    base: str
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    publication: PublicationResult


@dataclass(frozen=True)
class CleanupRequest:
    repository: str
    workspace: WorkspaceResult


@dataclass(frozen=True)
class CleanupResult:
    workspace: str


@dataclass(frozen=True)
class PrepareReviewRequest:
    event: ReviewEvent
    task_id: str
    workspace: str = ""


@dataclass(frozen=True)
class PrepareReviewResult:
    event: ReviewEvent
    task_id: str
    workspace: WorkspaceResult
    provider_state: dict[str, Any]


@dataclass(frozen=True)
class ExecuteReviewRequest:
    prepared: PrepareReviewResult
    prompt: str


@dataclass(frozen=True)
class ExecuteReviewResult:
    request: ReviewRequest
    review: ReviewResult


@dataclass(frozen=True)
class PublishReviewRequest:
    prepared: PrepareReviewResult
    execution: ExecuteReviewResult


@dataclass(frozen=True)
class PublishReviewResult:
    request: ReviewRequest


@dataclass(frozen=True)
class CleanupReviewRequest:
    prepared: PrepareReviewResult


@dataclass(frozen=True)
class CleanupReviewResult:
    workspace: str
