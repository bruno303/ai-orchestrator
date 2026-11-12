"""Reusable provider-neutral review operations."""

from __future__ import annotations

from orchestrator.domain import Context, PublishedReview, ReviewOutcome, ReviewTarget
from pathlib import Path
import shlex
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

_REVIEW_DESCRIPTION_LIMIT = 4000
_REVIEW_FILE_LIMIT = 50


def _bounded(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def review_prompt(target: ReviewTarget, *, workspace: str = "") -> str:
    """Build a review prompt scoped to one pull request.

    The target carries the immutable PR identity and changed-file list, so the
    reviewer does not need to discover the change by scanning the repository.
    """
    reference = target.url or target.id
    title = _bounded(target.title, 500) or "<untitled>"
    description = _bounded(target.description, _REVIEW_DESCRIPTION_LIMIT) or "(no description)"
    files = list(target.changed_files)
    shown_files = files[:_REVIEW_FILE_LIMIT]
    file_list = "\n".join(f"- {path}" for path in shown_files) or (
        "- (no file list returned; use `git diff --name-status` below)"
    )
    if len(files) > len(shown_files):
        file_list += f"\n- ... and {len(files) - len(shown_files)} more files"

    base_ref = f"origin/{target.target_ref}" if target.target_ref else "HEAD^"
    revision = target.revision or "<unknown>"
    if target.target_ref:
        stat_command = f"`git diff --stat {base_ref}...HEAD`"
        names_command = f"`git diff --name-status {base_ref}...HEAD`"
        if shown_files and len(files) <= _REVIEW_FILE_LIMIT:
            paths = " ".join(shlex.quote(path) for path in shown_files)
            diff_command = f"`git diff {base_ref}...HEAD -- {paths}`"
        else:
            diff_command = f"`git diff {base_ref}...HEAD`"
    else:
        stat_command = "`git diff --stat HEAD^`"
        names_command = "`git diff --name-status HEAD^`"
        diff_command = "`git diff HEAD^`"

    return f"""Review only pull request {reference} in {target.repository}.

Title: {title}
Base: {target.target_ref or "<unknown>"} -> Head: {target.source_ref or "<unknown>"}
Head revision: {revision}
Workspace: {workspace or "the current workspace"}

{description}

Changed files:
{file_list}

Before reviewing, run `git rev-parse HEAD`. If it does not equal {revision}, do
not review unrelated code; return valid JSON with verdict "comment" and a
summary explaining the workspace mismatch.

Scope the review to this pull request:
- {stat_command}
- {names_command}
- {diff_command}

Inspect the changed files and only the surrounding code needed to understand
them. Do not list or read the whole repository, do not inspect unrelated
commits, and do not review pre-existing code outside this diff. If the file
list is truncated, use the name-status command to enumerate the rest.

{REVIEW_PROMPT}"""


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
        prompt = request.prompt or review_prompt(
            prepared.target, workspace=prepared.workspace.workspace
        )
        review_request = ReviewRequest(
            task_id=prepared.target.id,
            repository=prepared.target.repository,
            workspace=prepared.workspace.workspace,
            prompt=prompt,
            context=prepared.context,
            log_file=str(self.task_log_path(prepared.target.id, request.node)),
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
