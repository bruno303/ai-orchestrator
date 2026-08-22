"""Reusable provider-neutral review operations."""

from __future__ import annotations

from orchestrator.domain import Context, PublishedReview, ReviewOutcome
from pathlib import Path
from typing import Callable

from orchestrator.application.ports import ReviewDestination, ReviewExecutor, ReviewRequest, WorkspaceManager, WorkspaceRequest, WorkspaceResult
from orchestrator.application.execution.errors import CleanupError, ReviewExecutionError, ReviewPublicationError, WorkspacePreparationError
from orchestrator.application.execution.models import (
    CleanupReviewRequest,
    CleanupReviewResult,
    ExecuteReviewRequest,
    ExecuteReviewResult,
    PrepareReviewRequest,
    PrepareReviewResult,
    PublishReviewRequest,
    PublishReviewResult,
)


# Generic review code passes Context without interpreting provider namespaces.
REVIEW_PROMPT = """Use only the `/code-review` skill to review this change. Follow its instructions, then return ONLY valid JSON with this schema:
{"verdict":"approve|request_changes|comment","summary":"...","findings":[{"message":"...","path":"optional","line":0,"side":"RIGHT|LEFT","severity":"info|warning|error"}],"checks":[{"name":"...","status":"pass|fail|skip"}]}
"""


class ReviewRuntime:
    def __init__(self, executor: ReviewExecutor, workspace_manager: WorkspaceManager, destination: ReviewDestination, *, task_log_path: Callable[[str, str], Path] | None = None) -> None:
        self.executor = executor
        self.workspace_manager = workspace_manager
        self.destination = destination
        self.task_log_path = task_log_path or (lambda task_id, node: Path(f"{task_id}-{node}.log"))

    def prepare(self, request: PrepareReviewRequest) -> PrepareReviewResult:
        target = request.target
        try:
            result = self.workspace_manager.prepare(WorkspaceRequest(
                task_id=target.id,
                repository=target.repository,
                branch=target.source_ref,
                base_branch=target.target_ref,
                purpose="review",
                target_ref=target.target_ref,
                revision=target.revision,
                checkout_mode="revision",
                workspace=request.workspace,
                context=target.context,
            ))
        except WorkspacePreparationError:
            raise
        except Exception as exc:
            raise WorkspacePreparationError(str(exc)) from exc
        result_context = getattr(result, "context", Context())
        context = target.context.merged(result_context)
        return PrepareReviewResult(
            target,
            WorkspaceResult(
                result.workspace, result.branch, result_context,
                getattr(result, "base_branch", ""),
            ),
            context,
        )

    def execute_review(self, request: ExecuteReviewRequest) -> ExecuteReviewResult:
        prepared = request.prepared
        review_request = ReviewRequest(
            task_id=prepared.target.id,
            repository=prepared.target.repository,
            workspace=prepared.workspace.workspace,
            prompt=request.prompt,
            context=prepared.context,
            log_file=str(self.task_log_path(prepared.target.id, "review")),
        )
        try:
            result = self.executor.execute(review_request)
        except Exception as exc:
            raise ReviewExecutionError(str(exc), context=prepared.context) from exc
        if isinstance(result, ReviewOutcome):
            outcome = result
        else:
            from orchestrator.domain import ReviewCheck, ReviewFinding
            comments = getattr(result, "comments", ())
            checks = getattr(result, "checks", ())
            outcome = ReviewOutcome(
                bool(result.success), result.verdict, result.summary,
                tuple(ReviewFinding(**finding) for finding in comments),
                tuple(ReviewCheck(**check) for check in checks if isinstance(check, dict)),
                prepared.context,
            )
        if not outcome.success:
            raise ReviewExecutionError(
                outcome.summary or "review execution failed", context=outcome.context
            )
        return ExecuteReviewResult(review_request, outcome)

    def publish_review(self, request: PublishReviewRequest) -> PublishReviewResult:
        try:
            result = self.destination.publish(request.prepared.target, request.execution.outcome)
        except Exception as exc:
            raise ReviewPublicationError(str(exc)) from exc
        if not isinstance(result, PublishedReview):
            result = PublishedReview(provider=getattr(self.destination, "provider_type", ""),
                                     context=request.execution.outcome.context)
        return PublishReviewResult(result)

    def cleanup_review(self, request: CleanupReviewRequest) -> CleanupReviewResult:
        try:
            self.workspace_manager.cleanup(request.prepared.workspace)
        except Exception as exc:
            raise CleanupError(str(exc)) from exc
        return CleanupReviewResult(request.prepared.workspace.workspace)
