"""Discussion stage catalog: detached checkout, read-only answer, publication.

The discussion loop keeps cleanup outside the graph as a guaranteed epilogue
(the same policy as review). These factories only expose the configurable
processing steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from orchestrator.application.discussion.service import (
    DiscussionResult,
    DiscussionRunRequest,
    DiscussionRuntime,
    PreparedDiscussion,
)
from orchestrator.application.execution.models import WorkContext
from orchestrator.application.ports import WorkspaceResult
from orchestrator.application.workflow import StepSpec
from orchestrator.domain import Context, WorkItem
from orchestrator.infra.langgraph.builder import NodeFn
from orchestrator.infra.langgraph.state import COMPLETED, PREPARING, DiscussionState


class WorkspaceNotPreparedError(RuntimeError):
    """A discussion answer started without a prepared checkout."""


def _item(state: DiscussionState) -> WorkItem:
    value = state.get("input") or {}
    data = value.get("data") or {}
    if data.get("id"):
        return WorkItem(
            data["id"], data["repository"], data.get("title", ""),
            data.get("description", ""), tuple(data.get("extra_context", ())),
            data.get("input_provider", value.get("provider", "")),
            Context.from_dict(data.get("context") or value.get("context") or {}),
        )
    raise ValueError("workflow input is missing canonical discussion data")


def _context(state: DiscussionState) -> Context:
    workspace_value = state.get("workspace") or {}
    if isinstance(workspace_value, dict) and workspace_value.get("context") is not None:
        return Context.from_dict(workspace_value["context"])
    value = (state.get("processing") or {}).get("context")
    if value is not None:
        return Context.from_dict(value)
    return _item(state).context


def _workspace(state: DiscussionState) -> dict[str, Any]:
    value = state.get("workspace") or {}
    if not isinstance(value, dict):
        value = {"path": value}
    result = dict(value)
    result.setdefault("path", "")
    result.setdefault("branch", "")
    result.setdefault("base_branch", "")
    result.setdefault("context", _context(state).to_dict())
    return result


def _request(state: DiscussionState) -> DiscussionRunRequest:
    work = WorkContext(_item(state))
    context = _item(state).context.merged(_context(state))
    git_context = context.namespace("git")
    workspace = _workspace(state)
    return DiscussionRunRequest(
        work=work,
        branch=workspace["branch"] or str(git_context.get("branch", "")),
        base_branch=workspace["base_branch"] or str(git_context.get("base_branch", "")),
        workspace=workspace["path"] or str(git_context.get("workspace", "")),
        revision=str(git_context.get("revision", "")),
        fetch_url=str(git_context.get("fetch_url", "")),
        context=context,
    )


def _prepared(state: DiscussionState) -> PreparedDiscussion:
    request = _request(state)
    workspace = _workspace(state)
    path = workspace["path"]
    if not path or not Path(path).exists():
        raise WorkspaceNotPreparedError(
            "discussion step requires a prepared workspace; run a checkout step first"
        )
    context = Context.from_dict(workspace.get("context") or request.context.to_dict())
    return PreparedDiscussion(
        request,
        WorkspaceResult(path, workspace["branch"], context, workspace["base_branch"]),
        context,
    )


def prepared_for_cleanup(
    state: DiscussionState, steps: list[StepSpec],
) -> PreparedDiscussion | None:
    """Return a prepared checkout only when a configured checkout step succeeded."""
    results = state.get("step_results") or {}
    if not any(
        step.type == "checkout" and results.get(step.name, {}).get("status") == "succeeded"
        for step in steps
    ):
        return None
    try:
        return _prepared(state)
    except (ValueError, WorkspaceNotPreparedError):
        return None


def cleanup_candidate(state: DiscussionState) -> PreparedDiscussion | None:
    """Return a cleanup-only workspace derived from the seeded state.

    The workspace carries the repository identity the manager needs to locate
    the base clone (``repo_dir`` is derived from it) and an empty branch, so a
    revision checkout never deletes the seeded local branch. This is a
    fallback for runs that raised before recording a checkout step (for
    example an interrupted answer). A missing workspace is a no-op.
    """
    try:
        prepared = _prepared(state)
        repository = _item(state).repository
    except (ValueError, WorkspaceNotPreparedError):
        return None
    context = prepared.context.merge_namespace("git", {"repository": repository})
    return PreparedDiscussion(
        prepared.request,
        WorkspaceResult(
            prepared.workspace.workspace, "", context, prepared.workspace.base_branch,
        ),
        context,
    )


@dataclass(frozen=True)
class DiscussionStageDeps:
    """Shared discussion runtime plus per-step overrides and prompt templates."""

    runtime: DiscussionRuntime
    step_runtimes: dict[str, DiscussionRuntime] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)

    def runtime_for(self, step: StepSpec) -> DiscussionRuntime:
        return self.step_runtimes.get(step.name, self.runtime)


def _checkout_node(step: StepSpec, deps: DiscussionStageDeps) -> NodeFn:
    def run(state: DiscussionState) -> dict[str, Any]:
        prepared = deps.runtime_for(step).prepare(_request(state))
        workspace = prepared.workspace
        print(
            f"[checkout] workspace={workspace.workspace} branch={workspace.branch}",
            flush=True,
        )
        return {
            "workspace": {
                "path": workspace.workspace,
                "branch": workspace.branch,
                "base_branch": workspace.base_branch,
                "context": prepared.context.to_dict(),
            },
            "status": PREPARING,
        }

    return run


def _discuss_node(step: StepSpec, deps: DiscussionStageDeps) -> NodeFn:
    def run(state: DiscussionState) -> dict[str, Any]:
        request = _request(state)
        prepared = _prepared(state)
        result = deps.runtime_for(step).answer(
            request, prepared, prompt=deps.prompts.get(step.name), node=step.name,
        )
        return {
            "processing": {"result": result},
            "step_results": {step.name: {"summary": result.response[:4000]}},
        }

    return run


def _publish_node(step: StepSpec, deps: DiscussionStageDeps) -> NodeFn:
    def run(state: DiscussionState) -> dict[str, Any]:
        result = (state.get("processing") or {}).get("result")
        if not isinstance(result, DiscussionResult):
            raise RuntimeError(
                f"discussion step '{step.name}' requires a prior discuss step"
            )
        deps.runtime_for(step).publish(_request(state), result)
        return {
            "status": COMPLETED,
            "output": {"response": result.response},
        }

    return run


STEP_FACTORIES: dict[str, Callable[[StepSpec, DiscussionStageDeps], NodeFn]] = {
    "checkout": _checkout_node,
    "discuss": _discuss_node,
    "publish": _publish_node,
}
