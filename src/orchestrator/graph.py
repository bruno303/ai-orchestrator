"""LangGraph workflow for provider-neutral work-item execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from orchestrator import state as state_mod
from orchestrator.domain import Context, WorkItem
from orchestrator.providers import Destination, ExecutionResult, Executor, WorkspaceManager, WorkspaceResult
from orchestrator.runtime import compose_execution_runtime
from orchestrator.runtime.errors import RuntimeOperationError
from orchestrator.runtime.execution import PLAN_FILE, implement_prompt as runtime_implement_prompt
from orchestrator.runtime.execution import plan_prompt as runtime_plan_prompt
from orchestrator.runtime.execution import test_prompt as runtime_test_prompt
from orchestrator.runtime.models import (
    AgentRequest,
    CleanupRequest,
    ImplementationRequest,
    PlanRequest,
    PrepareExecutionRequest,
    PublishRequest,
    TestRequest,
    WorkContext,
)
from orchestrator.state import TaskState


# Generic application/runtime code may pass and merge Context, but may not
# inspect provider-specific namespaces or keys.
def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fail(error: str) -> dict[str, Any]:
    return {"status": state_mod.FAILED, "error": error}


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
    for key in ("plan_path", "plan_summary", "implementation_result", "test_result"):
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


def _provider_name(component: Any, default: str) -> str:
    return str(getattr(component, "provider_type", getattr(component, "provider_name", default)))


def _runtime_error(exc: Exception) -> dict[str, Any]:
    updates = _fail(str(exc))
    if isinstance(exc, RuntimeOperationError):
        if exc.attempts is not None:
            updates["phase_attempts"] = exc.attempts
        if exc.context:
            try:
                updates["processing"] = {"context": exc.context.to_dict()}
            except (TypeError, ValueError):
                pass
    return updates


def _run_opencode(
    state: TaskState, node: str, agent: str, prompt: str, executor: Executor | None = None
) -> tuple[dict[str, Any], ExecutionResult | None]:
    runtime = compose_execution_runtime(executor=executor)
    try:
        result = runtime.execute_phase(
            AgentRequest(_work(state), node, agent, prompt, _workspace(state)["path"], _context(state))
        )
    except Exception as exc:
        return _runtime_error(exc), None
    return {
        "phase_attempts": result.attempts,
        "processing": _processing(state, {"context": result.context.to_dict()}),
    }, result.execution


def prepare_workspace(state: TaskState, manager: WorkspaceManager | None = None, runtime=None) -> dict[str, Any]:
    print(f"[{_now()}] prepare_workspace: starting", flush=True)
    try:
        current = _workspace(state)
        runtime = runtime or compose_execution_runtime(workspace_manager=manager)
        prepared = runtime.prepare(PrepareExecutionRequest(
            _work(state), current["branch"], current["base_branch"], current["path"], _context(state)
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
            "provider": _provider_name(manager or getattr(runtime, "workspace_manager", None), ""),
            "path": result.workspace,
            "branch": result.branch,
            "base_branch": prepared.base_branch,
            "context": prepared.context.to_dict(),
        },
        "status": state_mod.PREPARING,
    }


def plan(state: TaskState, executor: Executor | None = None, runtime=None) -> dict[str, Any]:
    try:
        runtime = runtime or compose_execution_runtime(executor=executor)
        result = runtime.plan(PlanRequest(_work(state), _workspace(state)["path"], _context(state)))
    except Exception as exc:
        return _runtime_error(exc)
    return {
        "status": state_mod.PLANNING,
        "processing": _processing(state, {
            "context": result.phase.context.to_dict(), "plan_path": result.plan_path,
            "plan_summary": result.summary,
        }),
        "phase_attempts": result.phase.attempts,
    }


def implement(state: TaskState, executor: Executor | None = None, runtime=None) -> dict[str, Any]:
    try:
        runtime = runtime or compose_execution_runtime(executor=executor)
        result = runtime.implement(ImplementationRequest(
            _work(state), _workspace(state)["path"], PLAN_FILE, _context(state)
        ))
    except Exception as exc:
        return _runtime_error(exc)
    return {
        "status": state_mod.IMPLEMENTING,
        "processing": _processing(state, {
            "context": result.phase.context.to_dict(), "implementation_result": result.summary,
        }),
        "phase_attempts": result.phase.attempts,
    }


def test(state: TaskState, executor: Executor | None = None, runtime=None) -> dict[str, Any]:
    try:
        runtime = runtime or compose_execution_runtime(executor=executor)
        result = runtime.test(TestRequest(_work(state), _workspace(state)["path"], _context(state)))
    except Exception as exc:
        return _runtime_error(exc)
    return {
        "status": state_mod.TESTING,
        "processing": _processing(state, {
            "context": result.phase.context.to_dict(), "test_result": result.summary,
        }),
        "phase_attempts": result.phase.attempts,
    }


def create_pr(state: TaskState, destination: Destination | None = None, runtime=None) -> dict[str, Any]:
    print(f"[{_now()}] publish: starting", flush=True)
    try:
        current = _workspace(state)
        runtime = runtime or compose_execution_runtime(destination=destination)
        publication = runtime.publish(PublishRequest(
            _work(state), current["path"], current["branch"], current["base_branch"], _context(state)
        )).publication
    except Exception as exc:
        print(f"[{_now()}] publish: ERROR {exc}", flush=True)
        return _runtime_error(exc)
    output: dict[str, Any] = {
        "provider": publication.provider or _provider_name(destination or getattr(runtime, "destination", None), ""),
        "context": publication.context.to_dict(),
    }
    if publication.id is not None:
        output["external_id"] = publication.id
    if publication.url is not None:
        output["url"] = publication.url
    return {"status": state_mod.COMPLETED, "output": output}


def cleanup(state: TaskState, manager: WorkspaceManager | None = None, runtime=None) -> dict[str, Any]:
    try:
        current = _workspace(state)
        runtime = runtime or compose_execution_runtime(workspace_manager=manager)
        runtime.cleanup(CleanupRequest(
            _work(state).repository,
            WorkspaceResult(
                current["path"], current["branch"],
                Context.from_dict(current["context"]), current["base_branch"],
            ),
        ))
    except Exception as exc:
        print(f"[{_now()}] cleanup: ERROR {exc}", flush=True)
    return {"status": state_mod.COMPLETED}


def plan_prompt(state: TaskState) -> str:
    return runtime_plan_prompt(_work(state))


def implement_prompt(state: TaskState) -> str:
    return runtime_implement_prompt(_work(state))


def test_prompt(state: TaskState) -> str:
    return runtime_test_prompt(_work(state))


def _route(next_node: str) -> Callable[[TaskState], str]:
    return lambda state: "end" if state.get("status") == state_mod.FAILED else next_node


def build_graph(
    on_node_start: Callable[[str, TaskState], None] | None = None,
    executor: Executor | None = None,
    workspace_manager: WorkspaceManager | None = None,
    destination: Destination | None = None,
    runtime=None,
):
    def guard(name: str, fn: Callable[[TaskState], dict[str, Any]]):
        def wrapped(state: TaskState) -> dict[str, Any]:
            if on_node_start is not None:
                try:
                    on_node_start(name, state)
                except Exception:
                    pass
            try:
                return fn(state)
            except Exception as exc:
                return _fail(f"unhandled exception in {name}: {exc}")
        return wrapped

    workflow_runtime = runtime or compose_execution_runtime(
        executor=executor, workspace_manager=workspace_manager, destination=destination
    )
    builder = StateGraph(TaskState)
    nodes = {
        "prepare_workspace": lambda value: prepare_workspace(value, runtime=workflow_runtime),
        "plan": lambda value: plan(value, runtime=workflow_runtime),
        "implement": lambda value: implement(value, runtime=workflow_runtime),
        "test": lambda value: test(value, runtime=workflow_runtime),
        "create_pr": lambda value: create_pr(value, runtime=workflow_runtime),
        "cleanup": lambda value: cleanup(value, runtime=workflow_runtime),
    }
    for name, fn in nodes.items():
        builder.add_node(name, guard(name, fn))
    builder.add_edge(START, "prepare_workspace")
    builder.add_conditional_edges("prepare_workspace", _route("plan"), {"plan": "plan", "end": END})
    builder.add_conditional_edges("plan", _route("implement"), {"implement": "implement", "end": END})
    builder.add_conditional_edges("implement", _route("test"), {"test": "test", "end": END})
    builder.add_conditional_edges("test", _route("create_pr"), {"create_pr": "create_pr", "end": END})
    builder.add_conditional_edges("create_pr", _route("cleanup"), {"cleanup": "cleanup", "end": END})
    builder.add_edge("cleanup", END)
    # Execution is deliberately process-local. GitHub publication markers are
    # the durable source of truth, not LangGraph checkpoints.
    return builder.compile()
