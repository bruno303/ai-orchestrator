"""Review stage catalog: checkout, structured review, publication.

Review cleanup deliberately stays in ``ReviewApplication.poll_once``'s
``finally`` block, outside the configurable workflow. Command steps are the
stage's generic escape hatch and run inside the prepared review checkout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from orchestrator.application.execution.models import (
    ExecuteReviewRequest,
    ExecuteReviewResult,
    PrepareReviewRequest,
    PrepareReviewResult,
    PublishReviewRequest,
)
from orchestrator.application.ports import ExecutionResult, WorkspaceResult
from orchestrator.application.review.service import REVIEW_PROMPT, ReviewRuntime
from orchestrator.application.workflow import StepSpec
from orchestrator.domain import Context, ReviewTarget
from orchestrator.infra.langgraph.builder import NodeFn
from orchestrator.infra.langgraph.state import COMPLETED, PREPARING, RECEIVED, ReviewState

_SNIPPET_LIMIT = 4000


def seed(target: ReviewTarget) -> dict[str, Any]:
    """Build the initial review graph state for one target."""
    return {
        "task_id": target.id,
        "input": {
            "provider": target.input_provider,
            "data": target.to_dict(),
        },
        "processing": {},
        "workspace": {},
        "output": {},
        "status": RECEIVED,
    }


def _target(state: ReviewState) -> ReviewTarget:
    value = state.get("input") or {}
    data = value.get("data") or {}
    if not data.get("id"):
        raise ValueError("workflow input is missing canonical review data")
    return ReviewTarget(
        id=data["id"],
        repository=data["repository"],
        title=data.get("title", ""),
        description=data.get("description", ""),
        source_ref=data.get("source_ref", ""),
        target_ref=data.get("target_ref", ""),
        revision=data.get("revision", ""),
        input_provider=data.get("input_provider", value.get("provider", "")),
        context=Context.from_dict(data.get("context") or value.get("context") or {}),
    )


def _context(state: ReviewState) -> Context:
    workspace_value = state.get("workspace") or {}
    if isinstance(workspace_value, dict) and workspace_value.get("context") is not None:
        return Context.from_dict(workspace_value["context"])
    return _target(state).context


def _workspace(state: ReviewState) -> dict[str, Any]:
    value = state.get("workspace") or {}
    if not isinstance(value, dict):
        value = {"path": value}
    result = dict(value)
    result.setdefault("path", "")
    result.setdefault("branch", "")
    result.setdefault("base_branch", "")
    result.setdefault("context", _context(state).to_dict())
    return result


def _prepared(state: ReviewState) -> PrepareReviewResult:
    target = _target(state)
    workspace = _workspace(state)
    path = workspace["path"]
    if not path or not Path(path).exists():
        raise RuntimeError(
            "review step requires a prepared workspace; run a checkout step first"
        )
    context = Context.from_dict(workspace.get("context") or target.context.to_dict())
    return PrepareReviewResult(
        target,
        WorkspaceResult(path, workspace["branch"], context, workspace["base_branch"]),
        context,
    )


def _output_summary(stdout: str, stderr: str = "") -> str:
    text = stdout[:_SNIPPET_LIMIT].rstrip("\n")
    if stderr:
        separator = "\nstderr:\n" if text else "stderr:\n"
        return (text + separator + stderr)[:_SNIPPET_LIMIT]
    return text


@dataclass(frozen=True)
class ReviewStageDeps:
    """Shared review runtime plus per-step overrides and prompt templates."""

    runtime: ReviewRuntime
    step_runtimes: dict[str, ReviewRuntime] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    command_runner: Callable[[str, str, int, str], ExecutionResult] | None = None
    command_timeout_seconds: int = 3600

    def runtime_for(self, step: StepSpec) -> ReviewRuntime:
        return self.step_runtimes.get(step.name, self.runtime)


def _checkout_node(step: StepSpec, deps: ReviewStageDeps) -> NodeFn:
    def run(state: ReviewState) -> dict[str, Any]:
        target = _target(state)
        prepared = deps.runtime_for(step).prepare(PrepareReviewRequest(target))
        workspace = prepared.workspace
        print(
            f"[review] workspace={workspace.workspace} branch={workspace.branch}",
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


def _review_node(step: StepSpec, deps: ReviewStageDeps) -> NodeFn:
    def run(state: ReviewState) -> dict[str, Any]:
        prepared = _prepared(state)
        execution = deps.runtime_for(step).execute_review(ExecuteReviewRequest(
            prepared,
            deps.prompts.get(step.name, REVIEW_PROMPT),
            node=step.name,
        ))
        return {
            "processing": {"execution": execution},
            "step_results": {
                step.name: {"summary": (execution.outcome.summary or "")[:_SNIPPET_LIMIT]}
            },
        }

    return run


def _publish_node(step: StepSpec, deps: ReviewStageDeps) -> NodeFn:
    def run(state: ReviewState) -> dict[str, Any]:
        execution = (state.get("processing") or {}).get("execution")
        if not isinstance(execution, ExecuteReviewResult):
            raise RuntimeError(
                f"review step '{step.name}' requires a prior review step"
            )
        publication = deps.runtime_for(step).publish_review(
            PublishReviewRequest(_prepared(state), execution)
        ).publication
        output: dict[str, Any] = {
            "provider": publication.provider,
            "context": publication.context.to_dict(),
        }
        if publication.id is not None:
            output["external_id"] = publication.id
        if publication.url is not None:
            output["url"] = publication.url
        return {"status": COMPLETED, "output": output}

    return run


def _command_node(step: StepSpec, deps: ReviewStageDeps) -> NodeFn:
    def run(state: ReviewState) -> dict[str, Any]:
        runtime = deps.runtime_for(step)
        workspace = _prepared(state).workspace.workspace
        if deps.command_runner is None:
            raise RuntimeError("command runner is not wired")
        result = deps.command_runner(
            workspace,
            step.command or "",
            step.timeout_seconds or deps.command_timeout_seconds,
            str(runtime.task_log_path(_target(state).id, step.name)),
        )
        if not result.success:
            raise RuntimeError(
                f"command step '{step.name}' failed (exit {result.exit_code})"
            )
        return {
            "step_results": {
                step.name: {"summary": _output_summary(result.stdout, result.stderr)}
            },
        }

    return run


STEP_FACTORIES: dict[str, Callable[[StepSpec, ReviewStageDeps], NodeFn]] = {
    "checkout": _checkout_node,
    "review": _review_node,
    "publish": _publish_node,
    "command": _command_node,
}
