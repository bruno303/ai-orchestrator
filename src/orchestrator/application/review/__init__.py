"""Provider-neutral pull-request review workflow adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typing import Callable

from orchestrator.application.ports import (
    ContextPresenter, NoopContextPresenter, ReviewDestination, ReviewExecutor,
    ReviewInputSource, WorkspaceManager, WorkspaceResult,
)
from orchestrator.domain import Context, ReviewTarget
from orchestrator.application.execution.models import (
    CleanupReviewRequest,
    ExecuteReviewRequest,
    PrepareReviewRequest,
    PrepareReviewResult,
    PublishReviewRequest,
)
from orchestrator.application.review.service import REVIEW_PROMPT, ReviewRuntime

_COMPLETED = "COMPLETED"


def _prepared_from_state(
    target: ReviewTarget, state: dict[str, Any],
) -> PrepareReviewResult | None:
    workspace = state.get("workspace") or {}
    if not isinstance(workspace, dict) or not workspace.get("path"):
        return None
    context = Context.from_dict(workspace.get("context") or target.context.to_dict())
    return PrepareReviewResult(
        target,
        WorkspaceResult(
            workspace["path"], workspace.get("branch", ""), context,
            workspace.get("base_branch", ""),
        ),
        context,
    )


def _cleanup_candidate(target: ReviewTarget) -> PrepareReviewResult | None:
    """Derive a cleanup-only prepared review from the target's git context.

    The candidate carries the repository identity the workspace manager needs
    to locate the base clone (``repo_dir`` is derived from it). Review
    checkouts are revision checkouts, so the branch stays empty (branch
    deletion disabled). This covers runs that raised before a prepared state
    existed, such as an interrupted graph runner.
    """
    git_context = target.context.namespace("git")
    workspace = str(git_context.get("workspace") or "")
    if not workspace:
        return None
    context = target.context.merge_namespace("git", {"repository": target.repository})
    return PrepareReviewResult(
        target,
        WorkspaceResult(workspace, "", context, str(git_context.get("base_branch") or "")),
        context,
    )


@dataclass(init=False)
class ReviewApplication:
    """Coordinate review events while delegating concrete steps to a runtime."""

    input_source: ReviewInputSource
    runtime: ReviewRuntime
    context_presenter: ContextPresenter
    run_workflow: Callable[[ReviewTarget], dict[str, Any]] | None

    def __init__(
        self,
        input_source: ReviewInputSource,
        executor: ReviewExecutor | None = None,
        workspace_manager: WorkspaceManager | None = None,
        destination: ReviewDestination | None = None,
        runtime: ReviewRuntime | None = None,
        context_presenter: ContextPresenter | None = None,
        write_task_log: Callable[[str, str, str], None] | None = None,
        run_workflow: Callable[[ReviewTarget], dict[str, Any]] | None = None,
    ) -> None:
        self.input_source = input_source
        self.runtime = runtime or ReviewRuntime(executor, workspace_manager, destination)
        self.context_presenter = context_presenter or getattr(
            input_source, "context_presenter", NoopContextPresenter()
        )
        self.write_task_log = write_task_log or (lambda _task_id, _node, _message: None)
        self.run_workflow = run_workflow

    def poll_once(self) -> list[ReviewTarget]:
        processed: list[ReviewTarget] = []
        for target in self.input_source.poll():
            task_id = target.id
            prepared = None
            fields = dict(self.context_presenter.logging_fields(target.context))
            title = " ".join(target.title.split()) or "<untitled>"
            self.write_task_log(
                task_id,
                "review",
                f"[review] starting: repository={target.repository} "
                f"id={target.id} revision={target.revision or '<unknown>'} "
                f"title={title!r} context={fields}",
            )
            print(
                f"[review] starting: repository={target.repository} "
                f"id={target.id} title={title!r}",
                flush=True,
            )
            try:
                if self.run_workflow is not None:
                    state = self.run_workflow(target)
                    prepared = _prepared_from_state(target, state)
                    if state.get("status") != _COMPLETED:
                        print(
                            f"[review] {target.id}: "
                            f"{state.get('error') or 'workflow did not complete'}",
                            flush=True,
                        )
                        continue
                else:
                    prepared = self.runtime.prepare(PrepareReviewRequest(target))
                    execution = self.runtime.execute_review(ExecuteReviewRequest(prepared, REVIEW_PROMPT))
                    self.runtime.publish_review(PublishReviewRequest(prepared, execution))
                processed.append(target)
                self.write_task_log(
                    task_id,
                    "review",
                    f"[review] finished: repository={target.repository} id={target.id}",
                )
                print(
                    f"[review] finished: repository={target.repository} id={target.id}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[review] {target.id}: {exc}", flush=True)
            finally:
                cleanup = prepared or _cleanup_candidate(target)
                if cleanup is not None:
                    try:
                        self.runtime.cleanup_review(CleanupReviewRequest(cleanup))
                    except Exception as exc:
                        print(f"[review] cleanup {target.id}: {exc}", flush=True)
        return processed
