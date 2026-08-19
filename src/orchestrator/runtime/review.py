"""Reusable runtime operations for pull-request reviews."""

from __future__ import annotations

from orchestrator import workspace
from orchestrator.providers import ReviewDestination, ReviewExecutor, ReviewRequest, WorkspaceManager, WorkspaceRequest, WorkspaceResult, validate_provider_state
from orchestrator.runtime.errors import CleanupError, ReviewExecutionError, ReviewPublicationError, WorkspacePreparationError
from orchestrator.runtime.models import (
    CleanupReviewRequest,
    CleanupReviewResult,
    ExecuteReviewRequest,
    ExecuteReviewResult,
    PrepareReviewRequest,
    PrepareReviewResult,
    PublishReviewRequest,
    PublishReviewResult,
)


REVIEW_PROMPT = """Review this pull request. Inspect the complete diff and repository context. Return ONLY valid JSON with this schema:
{"verdict":"approve|request_changes|comment","summary":"...","findings":[{"message":"...","path":"optional","line":0,"side":"RIGHT|LEFT","severity":"info|warning|error"}],"checks":[{"name":"...","status":"pass|fail|skip"}]}
Use inline locations only when they are on changed files. Do not invent findings."""


class ReviewRuntime:
    """Perform review preparation, execution, publication, and cleanup."""

    def __init__(self, executor: ReviewExecutor, workspace_manager: WorkspaceManager, destination: ReviewDestination) -> None:
        self.executor = executor
        self.workspace_manager = workspace_manager
        self.destination = destination

    def prepare(self, request: PrepareReviewRequest) -> PrepareReviewResult:
        provider_state = validate_provider_state(request.event.provider_state)
        number = provider_state.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ReviewExecutionError("review event is missing a pull-request number")
        workspace_path = request.workspace or str(workspace.review_workspace(request.event.repository, number))
        try:
            result = self.workspace_manager.prepare(
                WorkspaceRequest(
                    request.task_id,
                    request.event.repository,
                    provider_state.get("head_ref", ""),
                    provider_state.get("base_ref", ""),
                    {**provider_state, "workspace": workspace_path},
                    purpose="review",
                )
            )
            workspace_provider_state = validate_provider_state(result.provider_state)
        except WorkspacePreparationError:
            raise
        except Exception as exc:
            raise WorkspacePreparationError(str(exc)) from exc
        merged_provider_state = validate_provider_state({**provider_state, **workspace_provider_state})
        return PrepareReviewResult(
            request.event,
            request.task_id,
            WorkspaceResult(result.workspace, result.branch, workspace_provider_state),
            merged_provider_state,
        )

    def execute_review(self, request: ExecuteReviewRequest) -> ExecuteReviewResult:
        prepared = request.prepared
        provider_state = {
            **prepared.provider_state,
            "log_file": str(workspace.task_log_path(prepared.task_id, "review")),
        }
        review_request = ReviewRequest(
            prepared.task_id,
            prepared.event.repository,
            prepared.workspace.workspace,
            request.prompt,
            provider_state,
        )
        try:
            result = self.executor.execute(review_request)
        except Exception as exc:
            raise ReviewExecutionError(str(exc), provider_state=provider_state) from exc
        if not result.success:
            raise ReviewExecutionError(result.summary or "review execution failed", provider_state=result.provider_state)
        try:
            validate_provider_state(result.provider_state)
        except TypeError as exc:
            raise ReviewExecutionError(str(exc), provider_state=provider_state) from exc
        return ExecuteReviewResult(review_request, result)

    def publish_review(self, request: PublishReviewRequest) -> PublishReviewResult:
        try:
            self.destination.publish(request.execution.request, request.execution.review)
        except Exception as exc:
            raise ReviewPublicationError(str(exc)) from exc
        return PublishReviewResult(request.execution.request)

    def cleanup_review(self, request: CleanupReviewRequest) -> CleanupReviewResult:
        try:
            self.workspace_manager.cleanup(request.prepared.workspace)
        except Exception as exc:
            raise CleanupError(str(exc)) from exc
        return CleanupReviewResult(request.prepared.workspace.workspace)
