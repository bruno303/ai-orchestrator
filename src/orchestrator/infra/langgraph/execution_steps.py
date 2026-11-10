"""Execution and comment-implementation stage catalogs for the workflow builder.

Node factories map declarative :class:`StepSpec` entries onto the runtime
operations of :class:`ExecutionRuntime`; this node set is the generalization of
the original hardcoded execution chain in ``graph.py``. Generic code may pass
and merge ``Context``, but never inspects provider-specific namespaces or keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from orchestrator.infra.langgraph.builder import NodeFn
from orchestrator.infra.langgraph.state import (
    COMPLETED,
    FAILED,
    IMPLEMENTING,
    PLANNING,
    PREPARING,
    TaskState,
)
from orchestrator.domain import Context, WorkItem
from orchestrator.application.ports import WorkspaceResult
from orchestrator.application.execution.errors import RuntimeOperationError
from orchestrator.application.execution.service import (
    PLAN_FILE,
    ExecutionRuntime,
    implement_prompt as runtime_implement_prompt,
    plan_prompt as runtime_plan_prompt,
)
from orchestrator.application.execution.models import (
    AgentRequest,
    CleanupRequest,
    CommandStepRequest,
    ImplementationRequest,
    IncrementalImplementationRequest,
    PlanRequest,
    PrepareExecutionRequest,
    PublishRequest,
    WorkContext,
)
from orchestrator.application.workflow import StepSpec


_SNIPPET_LIMIT = 4000


class WorkspaceNotPreparedError(RuntimeError):
    """A workspace-consuming step started without a previously prepared workspace."""

# Generic application/runtime code may pass and merge Context, but may not
# inspect provider-specific namespaces or keys.
def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fail(error: str) -> dict[str, Any]:
    return {"status": FAILED, "error": error}


def _item(state: TaskState) -> WorkItem:
    value = state.get("input") or {}
    data = value.get("data") or {}
    if data.get("id"):
        return WorkItem(
            data["id"], data["repository"], data.get("title", ""),
            data.get("description", ""), tuple(data.get("extra_context", ())),
            data.get("input_provider", value.get("provider", "")),
            Context.from_dict(data.get("context") or value.get("context") or {}),
        )
    raise ValueError("workflow input is missing canonical work item data")


def _work(state: TaskState) -> WorkContext:
    return WorkContext(_item(state))


def _context(state: TaskState) -> Context:
    processing = state.get("processing") or {}
    value = processing.get("context")
    if value is not None:
        return Context.from_dict(value)
    workspace_value = state.get("workspace") or {}
    if isinstance(workspace_value, dict) and workspace_value.get("context") is not None:
        return Context.from_dict(workspace_value["context"])
    return _item(state).context


def _processing(state: TaskState, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(state.get("processing") or {})
    if updates:
        result.update(updates)
    for key in ("plan_path", "plan_summary", "implementation_result"):
        if key not in result and state.get(key) is not None:
            result[key] = state[key]
    return result


def _workspace(state: TaskState) -> dict[str, Any]:
    value = state.get("workspace") or {}
    if not isinstance(value, dict):
        value = {"path": value}
    result = dict(value)
    result.setdefault("path", "")
    result.setdefault("branch", "")
    result.setdefault("base_branch", "")
    if not result["path"]:
        result["path"] = state.get("workspace_path", "")
    if not result["branch"]:
        result["branch"] = state.get("branch", "")
    if not result["base_branch"]:
        result["base_branch"] = state.get("base_branch", "")
    result.setdefault("context", _context(state).to_dict())
    return result


def _prepared_workspace(state: TaskState, step_name: str) -> dict[str, Any]:
    """Resolve the workspace state of a step that consumes a prepared workspace.

    Raises :class:`WorkspaceNotPreparedError` when the path is empty or does not
    exist: an empty path would resolve to ``Path("")`` (``.``), silently running
    agent and command steps inside the orchestrator's own working directory.
    Only the ``checkout`` step may start without a prepared workspace.
    """
    current = _workspace(state)
    path = current["path"]
    if not path or not Path(path).exists():
        raise WorkspaceNotPreparedError(
            f"step '{step_name}' requires a prepared workspace; run a checkout step first"
        )
    return current


def _provider_name(component: Any, default: str) -> str:
    return str(getattr(component, "provider_type", getattr(component, "provider_name", default)))


def _runtime_error(exc: Exception) -> dict[str, Any]:
    updates = _fail(str(exc))
    if isinstance(exc, RuntimeOperationError):
        if exc.context:
            try:
                updates["processing"] = {"context": exc.context.to_dict()}
            except (TypeError, ValueError):
                pass
    return updates


def _comment_instruction(state: TaskState) -> str:
    """The comment-impl instruction, mirrored from ``cli._comment_instruction``."""
    event = (state.get("input") or {}).get("event") or {}
    metadata = event.get("metadata") or {}
    return str(metadata.get("instruction", ""))


def _output_summary(stdout: str, stderr: str = "") -> str:
    """Captured output snippet for step_results extras, capped at 4000 characters."""
    text = stdout[:_SNIPPET_LIMIT].rstrip("\n")
    if stderr:
        separator = "\nstderr:\n" if text else "stderr:\n"
        return (text + separator + stderr)[:_SNIPPET_LIMIT]
    return text


@dataclass(frozen=True)
class ExecutionStageDeps:
    """Stage bundle passed through to every execution-stage node factory.

    ``step_runtimes`` overrides the stage runtime for individual step names
    (per-step executor/model variants); ``prompts`` maps step names to loaded
    prompt-file template text; ``reuse_workspace`` belongs to the comment-impl
    path (Phase 1 wires it False for the execution stage).
    """

    runtime: ExecutionRuntime
    step_runtimes: dict[str, ExecutionRuntime] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    command_timeout_seconds: int = 3600
    reuse_workspace: bool = False

    def runtime_for(self, step: StepSpec) -> ExecutionRuntime:
        """Return the per-step override runtime or the stage default."""
        return self.step_runtimes.get(step.name, self.runtime)


def prepare_workspace(
    state: TaskState,
    runtime: ExecutionRuntime,
    reuse_workspace: bool = False,
) -> dict[str, Any]:
    print(f"[{_now()}] prepare_workspace: starting", flush=True)
    try:
        current = _workspace(state)
        prepared = runtime.prepare(PrepareExecutionRequest(
            _work(state), current["branch"], current["base_branch"], current["path"], _context(state),
            reuse_workspace=reuse_workspace,
        ))
    except Exception as exc:
        print(f"[{_now()}] prepare_workspace: ERROR {exc}", flush=True)
        return _runtime_error(exc)
    result = prepared.workspace
    print(
        f"[{_now()}] prepare_workspace: workspace={result.workspace} "
        f"branch={result.branch} base={prepared.base_branch}", flush=True,
    )
    return {
        "workspace": {
            "provider": _provider_name(runtime.workspace_manager, ""),
            "path": result.workspace,
            "branch": result.branch,
            "base_branch": prepared.base_branch,
            "context": prepared.context.to_dict(),
        },
        "status": PREPARING,
    }


def plan(state: TaskState, runtime: ExecutionRuntime, prompt: str | None = None,
         *, step_name: str) -> dict[str, Any]:
    try:
        result = runtime.plan(PlanRequest(
            _work(state), _prepared_workspace(state, step_name)["path"], _context(state), prompt=prompt,
        ))
    except Exception as exc:
        return _runtime_error(exc)
    return {
        "status": PLANNING,
        "processing": _processing(state, {
            "context": result.phase.context.to_dict(), "plan_path": result.plan_path,
            "plan_summary": result.summary,
        }),
    }


def implement(state: TaskState, runtime: ExecutionRuntime, prompt: str | None = None,
              *, step_name: str) -> dict[str, Any]:
    try:
        result = runtime.implement(ImplementationRequest(
            _work(state), _prepared_workspace(state, step_name)["path"], PLAN_FILE, _context(state), prompt=prompt,
        ))
    except Exception as exc:
        return _runtime_error(exc)
    return {
        "status": IMPLEMENTING,
        "processing": _processing(state, {
            "context": result.phase.context.to_dict(), "implementation_result": result.summary,
        }),
    }


def implement_comment(state: TaskState, runtime: ExecutionRuntime, *,
                      step_name: str) -> dict[str, Any]:
    try:
        result = runtime.implement_incremental(IncrementalImplementationRequest(
            _work(state), _prepared_workspace(state, step_name)["path"], _comment_instruction(state), _context(state),
        ))
    except Exception as exc:
        return _runtime_error(exc)
    return {
        "status": IMPLEMENTING,
        "processing": _processing(state, {
            "context": result.phase.context.to_dict(), "implementation_result": result.summary,
        }),
    }


def create_pr(state: TaskState, runtime: ExecutionRuntime, *, step_name: str) -> dict[str, Any]:
    print(f"[{_now()}] publish: starting", flush=True)
    try:
        current = _prepared_workspace(state, step_name)
        publication = runtime.publish(PublishRequest(
            _work(state), current["path"], current["branch"], current["base_branch"], _context(state)
        )).publication
    except Exception as exc:
        print(f"[{_now()}] publish: ERROR {exc}", flush=True)
        return _runtime_error(exc)
    output: dict[str, Any] = {
        "provider": publication.provider or _provider_name(runtime.destination, ""),
        "context": publication.context.to_dict(),
    }
    if publication.id is not None:
        output["external_id"] = publication.id
    if publication.url is not None:
        output["url"] = publication.url
    return {"status": COMPLETED, "output": output}


def cleanup(state: TaskState, runtime: ExecutionRuntime, *, step_name: str) -> dict[str, Any]:
    try:
        current = _prepared_workspace(state, step_name)
        runtime.cleanup(CleanupRequest(
            _work(state).repository,
            WorkspaceResult(
                current["path"], current["branch"],
                Context.from_dict(current["context"]), current["base_branch"],
            ),
        ))
    except Exception as exc:
        print(f"[{_now()}] cleanup: ERROR {exc}", flush=True)
        # Failures are recorded by the builder's wrapper, which applies the
        # step's on_failure policy; the old graph swallowed them here.
        raise
    return {"status": COMPLETED}


def _command_step(state: TaskState, deps: ExecutionStageDeps, step: StepSpec) -> dict[str, Any]:
    result = deps.runtime_for(step).run_command(CommandStepRequest(
        _work(state), step.name, _prepared_workspace(state, step.name)["path"], step.command,
        step.timeout_seconds or deps.command_timeout_seconds, _context(state),
    )).result
    if not result.success:
        raise RuntimeError(f"command step '{step.name}' failed (exit {result.exit_code})")
    return {
        "processing": _processing(state, {"context": result.context.to_dict()}),
        "step_results": {step.name: {"summary": _output_summary(result.stdout, result.stderr)}},
    }


def _agent_step(state: TaskState, deps: ExecutionStageDeps, step: StepSpec) -> dict[str, Any]:
    try:
        template = deps.prompts[step.name]
    except KeyError:
        # Defensive for directly-built graphs: configuration loading rejects
        # prompt-less `agent` steps before a workflow ever runs.
        raise ValueError(
            f"agent step '{step.name}' has no prompt template "
            f"(set the step's 'prompt_file', or provide deps.prompts['{step.name}'])"
        ) from None
    work = _work(state)
    try:
        rendered = template.format(
            task_id=work.task_id,
            repository=work.repository,
            title=work.title,
            description=work.body,
            extra_context="\n".join(work.extra_context),
        )
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        placeholder = exc.args[0] if exc.args else exc
        raise ValueError(
            f"agent step '{step.name}': invalid prompt placeholder {placeholder!r}; "
            "escape literal braces by doubling them: '{{' renders '{' and "
            "'}}' renders '}'"
        ) from None
    phase = deps.runtime_for(step).execute_phase(AgentRequest(
        work, step.name, step.agent or "build", rendered,
        _prepared_workspace(state, step.name)["path"], _context(state),
    ))
    return {
        "processing": _processing(state, {"context": phase.context.to_dict()}),
        "step_results": {step.name: {"summary": _output_summary(phase.execution.stdout)}},
    }


def plan_prompt(state: TaskState) -> str:
    return runtime_plan_prompt(_work(state))


def implement_prompt(state: TaskState) -> str:
    return runtime_implement_prompt(_work(state))


def _checkout_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return prepare_workspace(
            state, deps.runtime_for(step), reuse_workspace=deps.reuse_workspace
        )

    return run


def _plan_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return plan(
            state, deps.runtime_for(step), prompt=deps.prompts.get(step.name), step_name=step.name
        )

    return run


def _implement_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return implement(
            state, deps.runtime_for(step), prompt=deps.prompts.get(step.name), step_name=step.name
        )

    return run


def _implement_comment_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return implement_comment(state, deps.runtime_for(step), step_name=step.name)

    return run


def _publish_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return create_pr(state, deps.runtime_for(step), step_name=step.name)

    return run


def _cleanup_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return cleanup(state, deps.runtime_for(step), step_name=step.name)

    return run


def _command_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return _command_step(state, deps, step)

    return run


def _agent_node(step: StepSpec, deps: ExecutionStageDeps) -> NodeFn:
    def run(state: TaskState) -> dict[str, Any]:
        return _agent_step(state, deps, step)

    return run


STEP_FACTORIES: dict[str, Callable[[StepSpec, ExecutionStageDeps], NodeFn]] = {
    "checkout": _checkout_node,
    "plan": _plan_node,
    "implement": _implement_node,
    "implement_comment": _implement_comment_node,
    "publish": _publish_node,
    "cleanup": _cleanup_node,
    "command": _command_node,
    "agent": _agent_node,
}
