"""Provider-neutral runtime for read-only discussion comments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator.application.execution.errors import AgentExecutionError, WorkspacePreparationError
from orchestrator.application.execution.models import WorkContext
from orchestrator.application.ports import (
    DiscussionDestination,
    DiscussionExecutor,
    DiscussionPublicationRequest,
    DiscussionRequest,
    DiscussionResult,
    WorkspaceManager,
    WorkspaceRequest,
    WorkspaceResult,
)
from orchestrator.domain import Context


def _extra_context(work: WorkContext) -> str:
    if not work.extra_context:
        return ""
    blocks = "\n\n".join(f"<context>\n{text}\n</context>" for text in work.extra_context)
    return f"\n\nAdditional issue/PR/comment context:\n{blocks}"


def discussion_prompt(work: WorkContext) -> str:
    """Build a plain-text analysis prompt with an explicit read-only contract."""
    return f"""Answer the discussion question for work item {work.task_id} in repository {work.repository}.

Issue title:
{work.title}

Issue description:
{work.body}
{_extra_context(work)}

This is the read-only `/ai-agent-discuss` workflow. Inspect the repository and
the supplied issue, pull-request, and comment context as needed, then answer
the triggering question in clear Markdown. Do not modify files, create a
commit, push a branch, create or update a pull request, run implementation or
planning workflows, or invoke `/plan-implementation`. Return only the answer
that should be posted back to the originating GitHub conversation.
"""


@dataclass(frozen=True)
class DiscussionRunRequest:
    work: WorkContext
    branch: str = ""
    base_branch: str = ""
    workspace: str = ""
    revision: str = ""
    fetch_url: str = ""
    context: Context = field(default_factory=Context)


class DiscussionRuntime:
    """Prepare a detached checkout, run a read-only agent, and publish text."""

    def __init__(
        self,
        executor: DiscussionExecutor,
        workspace_manager: WorkspaceManager,
        destination: DiscussionDestination,
        *,
        repository_allowed: Callable[[str], bool] = lambda _repository: True,
        model_config=None,
        task_log_path: Callable[[str, str], Path] | None = None,
    ) -> None:
        self.executor = executor
        self.workspace_manager = workspace_manager
        self.destination = destination
        self.repository_allowed = repository_allowed
        self.model_config = model_config
        self.task_log_path = task_log_path or (lambda task_id, node: Path(f"{task_id}-{node}.log"))

    def run(self, request: DiscussionRunRequest) -> DiscussionResult:
        if not self.repository_allowed(request.work.repository):
            raise WorkspacePreparationError(
                f"repository {request.work.repository} is not in the allowlist"
            )

        context = request.work.item.context.merged(request.context)
        git_context = context.namespace("git")
        try:
            prepared = self.workspace_manager.prepare(WorkspaceRequest(
                task_id=request.work.task_id,
                repository=request.work.repository,
                branch=request.branch,
                base_branch=request.base_branch or str(git_context.get("base_branch", "")),
                purpose="discussion",
                repository_url=str(git_context.get("repository_url", "")),
                fetch_url=request.fetch_url or str(git_context.get("fetch_url", "")),
                target_ref=request.base_branch or str(git_context.get("base_branch", "")),
                revision=request.revision or str(git_context.get("revision", "")),
                checkout_mode="revision",
                workspace=request.workspace or str(git_context.get("workspace", "")),
                context=context,
            ))
        except WorkspacePreparationError:
            raise
        except Exception as exc:
            raise WorkspacePreparationError(str(exc)) from exc

        prepared_context = context.merged(getattr(prepared, "context", Context()))
        model = self.model_config.name if self.model_config else None
        variant = self.model_config.variant if self.model_config else None
        try:
            result = self.executor.execute(DiscussionRequest(
                task_id=request.work.task_id,
                repository=request.work.repository,
                workspace=prepared.workspace,
                prompt=discussion_prompt(request.work),
                model=model,
                variant=variant,
                context=prepared_context,
                log_file=str(self.task_log_path(request.work.task_id, "discussion")),
            ))
            if not isinstance(result, DiscussionResult):
                result = DiscussionResult(
                    bool(getattr(result, "success", False)),
                    str(getattr(result, "response", "")),
                    context=prepared_context,
                )
            if not result.success:
                raise AgentExecutionError(
                    result.response or result.stderr or "discussion executor failed",
                    context=result.context or prepared_context,
                )
            response = result.response.strip()
            if not response:
                raise AgentExecutionError("discussion executor returned an empty response", context=prepared_context)
            result_context = prepared_context.merged(result.context)
            self.destination.publish(DiscussionPublicationRequest(
                task_id=request.work.task_id,
                repository=request.work.repository,
                response=response,
                context=result_context,
            ))
            return DiscussionResult(
                True,
                response=response,
                duration_seconds=result.duration_seconds,
                context=result_context,
            )
        finally:
            try:
                self.workspace_manager.cleanup(prepared)
            except Exception:
                # A discussion has no local changes to preserve. Cleanup is
                # best effort and must not hide an already published answer.
                pass
