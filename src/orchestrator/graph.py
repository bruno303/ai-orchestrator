"""LangGraph workflow: Issue -> workspace -> plan -> implement -> test -> PR."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from orchestrator import state as state_mod, workspace
from orchestrator.providers import (
    Destination, ExecutionResult, Executor, WorkspaceManager, WorkspaceResult, validate_provider_state,
)
from orchestrator.runtime import compose_execution_runtime
from orchestrator.runtime.errors import RuntimeOperationError
from orchestrator.runtime.execution import PLAN_FILE, plan_prompt as runtime_plan_prompt
from orchestrator.runtime.execution import implement_prompt as runtime_implement_prompt
from orchestrator.runtime.execution import pr_body as runtime_pr_body
from orchestrator.runtime.execution import test_prompt as runtime_test_prompt
from orchestrator.runtime.models import (
    AgentRequest,
    CleanupRequest,
    ImplementationRequest,
    IssueContext,
    PlanRequest,
    PrepareExecutionRequest,
    PublishRequest,
    TestRequest,
)
from orchestrator.state import TaskState

def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fail(state: TaskState, error: str) -> dict[str, Any]:
    return {"status": state_mod.FAILED, "error": error}


def _input(state: TaskState) -> dict[str, Any]:
    value = state.get("input") or {}
    data = value.get("data") or {}
    provider_state = validate_provider_state(value.get("provider_state", {}))
    if not provider_state and state.get("provider_state"):
        provider_state = validate_provider_state(state["provider_state"])
    issue_number = data.get("number", data.get("issue_number", state.get("issue_number")))
    if issue_number is not None and "source_number" not in provider_state:
        # Compatibility for checkpoints created before provider-owned state.
        provider_state = {**provider_state, "source_number": issue_number}
    return {
        "provider": value.get("provider", "github"),
        "provider_state": provider_state,
        "repository": data.get("repository", state.get("repository", "")),
        "work_item_id": data.get("work_item_id", state.get("task_id", "")),
        "issue_number": issue_number,
        "issue_title": data.get("title", data.get("issue_title", state.get("issue_title", ""))),
        "issue_body": data.get("body", data.get("issue_body", state.get("issue_body", ""))),
        "extra_context": data.get("extra_context", state.get("extra_context", [])),
    }


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
    fallbacks = (("path", "workspace_path"), ("branch", "branch"), ("base_branch", "base_branch"),
                 ("provider_state", "provider_state"))
    for key, legacy in fallbacks:
        if key not in result and state.get(legacy) is not None and not isinstance(state.get(legacy), dict):
            result[key] = state[legacy]
    result.setdefault("path", "")
    result.setdefault("branch", "")
    result.setdefault("base_branch", "")
    if "provider_state" not in result:
        result["provider_state"] = {}
    return result


def _provider_name(component: Any, default: str) -> str:
    return str(getattr(component, "provider_type", getattr(component, "provider_name", default)))


def _context(state: TaskState) -> IssueContext:
    source = _input(state)
    return IssueContext(
        task_id=state.get("task_id", f"{source['repository']}#{source['issue_number']}"),
        repository=source["repository"],
        issue_number=source["issue_number"],
        work_item_id=source["work_item_id"],
        title=source["issue_title"],
        body=source["issue_body"],
        extra_context=source["extra_context"],
        provider_state=source["provider_state"],
    )


def _processing_provider_state(state: TaskState) -> dict[str, Any]:
    return validate_provider_state(_processing(state).get("provider_state", {}))


def _runtime_error(state: TaskState, exc: Exception) -> dict[str, Any]:
    updates = _fail(state, str(exc))
    if isinstance(exc, RuntimeOperationError):
        if exc.attempts is not None:
            updates["phase_attempts"] = exc.attempts
        if exc.provider_state:
            updates["processing"] = _processing(state, {"provider_state": exc.provider_state})
    return updates


def _namespace_updates(state: TaskState, *, input_data: dict[str, Any] | None = None,
                      processing: dict[str, Any] | None = None,
                      workspace_data: dict[str, Any] | None = None,
                      output: dict[str, Any] | None = None) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if input_data is not None:
        updates["input"] = input_data
    if processing is not None:
        updates["processing"] = processing
    if workspace_data is not None:
        updates["workspace"] = workspace_data
    if output is not None:
        updates["output"] = output
    return updates


def _run_opencode(
    state: TaskState, node: str, agent: str, prompt: str, executor: Executor | None = None
) -> tuple[dict[str, Any], ExecutionResult | None]:
    runtime = compose_execution_runtime(executor=executor)
    try:
        result = runtime.execute_phase(
            AgentRequest(_context(state), node, agent, prompt, _workspace(state)["path"], _processing_provider_state(state))
        )
    except Exception as exc:
        return _runtime_error(state, exc), None
    return {"phase_attempts": result.attempts, "processing": _processing(state, {"provider_state": result.provider_state})}, result.execution


# ---------------------------------------------------------------- nodes


def prepare_workspace(state: TaskState, manager: WorkspaceManager | None = None, runtime=None) -> dict[str, Any]:
    print(f"[{_now()}] prepare_workspace: starting", flush=True)
    source = _input(state)
    repository = source["repository"]
    try:
        current_workspace = _workspace(state)
        branch = current_workspace["branch"] or source["provider_state"].get("branch", "")
        ws = str(current_workspace["path"] or source["provider_state"].get("workspace", ""))
        if not branch or not ws:
            if source["issue_number"] is None:
                raise ValueError("work item is missing explicit workspace checkout instructions")
            branch = branch or f"ai/issue-{source['issue_number']}"
            ws = ws or str(workspace.task_workspace(repository, source["issue_number"]))
        runtime = runtime or compose_execution_runtime(workspace_manager=manager)
        prepared = runtime.prepare(
            PrepareExecutionRequest(
                _context(state), branch, current_workspace["base_branch"], ws,
                {**current_workspace["provider_state"], "repository_url": source["provider_state"].get("repository_url", state.get("repository_url"))},
            )
        )
    except Exception as exc:
        print(f"[{_now()}] prepare_workspace: ERROR {exc}", flush=True)
        return _runtime_error(state, exc)
    result = prepared.workspace
    provider_state = result.provider_state
    resolved_base_branch = prepared.base_branch
    print(
        f"[{_now()}] prepare_workspace: workspace={result.workspace} branch={result.branch} base={resolved_base_branch}",
        flush=True,
    )
    input_value = state.get("input") or {}
    input_data = dict(input_value.get("data") or {})
    input_data.update({"repository": source["repository"], "number": source["issue_number"],
                       "work_item_id": source["work_item_id"],
                       "title": source["issue_title"], "body": source["issue_body"],
                       "extra_context": source["extra_context"]})
    return {
        **_namespace_updates(
            state,
            input_data={"provider": source["provider"], "provider_state": source["provider_state"],
                        "data": input_data},
            workspace_data={"provider": _provider_name(manager or getattr(runtime, "workspace_manager", None), "git"), "path": result.workspace, "branch": result.branch,
                            "base_branch": resolved_base_branch,
                            "provider_state": provider_state},
        ),
        "status": state_mod.PREPARING,
    }


def plan(state: TaskState, executor: Executor | None = None, runtime=None) -> dict[str, Any]:
    try:
        runtime = runtime or compose_execution_runtime(executor=executor)
        result = runtime.plan(PlanRequest(_context(state), _workspace(state)["path"], _processing_provider_state(state)))
    except Exception as exc:
        return _runtime_error(state, exc)
    processing = {**_processing(state, {"provider_state": result.phase.provider_state}), "plan_path": result.plan_path, "plan_summary": result.summary}
    return {
        "status": state_mod.PLANNING,
        "processing": processing,
        "phase_attempts": result.phase.attempts,
    }


def implement(state: TaskState, executor: Executor | None = None, runtime=None) -> dict[str, Any]:
    try:
        runtime = runtime or compose_execution_runtime(executor=executor)
        result = runtime.implement(ImplementationRequest(_context(state), _workspace(state)["path"], PLAN_FILE, _processing_provider_state(state)))
    except Exception as exc:
        return _runtime_error(state, exc)
    processing = {**_processing(state, {"provider_state": result.phase.provider_state}), "implementation_result": result.summary}
    return {
        "status": state_mod.IMPLEMENTING,
        "processing": processing,
        "phase_attempts": result.phase.attempts,
    }


def test(state: TaskState, executor: Executor | None = None, runtime=None) -> dict[str, Any]:
    try:
        runtime = runtime or compose_execution_runtime(executor=executor)
        result = runtime.test(TestRequest(_context(state), _workspace(state)["path"], _processing_provider_state(state)))
    except Exception as exc:
        return _runtime_error(state, exc)
    processing = {**_processing(state, {"provider_state": result.phase.provider_state}), "test_result": result.summary}
    return {
        "status": state_mod.TESTING,
        "processing": processing,
        "phase_attempts": result.phase.attempts,
    }


def create_pr(state: TaskState, destination: Destination | None = None, runtime=None) -> dict[str, Any]:
    print(f"[{_now()}] create_pr: starting", flush=True)
    try:
        source = _input(state)
        workspace_state = _workspace(state)
        title = f"feat: {source['issue_title']}"[:72]
        runtime = runtime or compose_execution_runtime(destination=destination)
        published = runtime.publish(
            PublishRequest(_context(state), workspace_state["path"], workspace_state["branch"], workspace_state["base_branch"])
        )
        result = published.publication
        pr_number = result.number
    except Exception as exc:
        print(f"[{_now()}] create_pr: ERROR {exc}", flush=True)
        return _runtime_error(state, exc)
    print(f"[{_now()}] create_pr: PR #{pr_number} created", flush=True)
    publication_state = validate_provider_state(result.provider_state)
    output = dict(state.get("output") or {})
    output.update({"provider": _provider_name(destination or getattr(runtime, "destination", None), "github"),
                   "provider_state": {**publication_state, "pr_number": pr_number}})
    if result.url is not None:
        output["url"] = result.url
    return {"status": state_mod.COMPLETED, "output": output}


def cleanup(state: TaskState, manager: WorkspaceManager | None = None, runtime=None) -> dict[str, Any]:
    """Remove the task worktree and branch after the PR was created.

    Runs only on success; failed tasks keep their worktree for debugging.
    Cleanup problems are logged but never fail the task.
    """
    print(f"[{_now()}] cleanup: starting", flush=True)
    try:
        workspace_state = _workspace(state)
        runtime = runtime or compose_execution_runtime(workspace_manager=manager)
        runtime.cleanup(
            CleanupRequest(
                _input(state)["repository"],
                WorkspaceResult(
                    workspace_state["path"], workspace_state["branch"],
                    workspace_state.get("provider_state") or {},
                ),
            )
        )
    except Exception as exc:
        print(f"[{_now()}] cleanup: ERROR {exc}", flush=True)
        return {"status": state_mod.COMPLETED}
    print(f"[{_now()}] cleanup: removed {_workspace(state)['path']} (branch {_workspace(state)['branch']})", flush=True)
    return {"status": state_mod.COMPLETED}


# ---------------------------------------------------------------- prompts


def _extra_context(state: TaskState) -> str:
    extra_context = _input(state)["extra_context"]
    if not extra_context:
        return ""
    blocks = "\n\n".join(f"<comment>\n{text}\n</comment>" for text in extra_context)
    return f"\n\nAdditional requirements from comments:\n{blocks}"


def plan_prompt(state: TaskState) -> str:
    return runtime_plan_prompt(_context(state))


def implement_prompt(state: TaskState) -> str:
    return runtime_implement_prompt(_context(state))


def test_prompt(state: TaskState) -> str:
    return runtime_test_prompt(_context(state))


# ---------------------------------------------------------------- helpers


def _pr_body(state: TaskState, current_body: str | None = None) -> str:
    """Build the PR body with the issue-closing reference first.

    The `Closes #n` line is always the first line; matching lines in
    `current_body` are removed so it is never duplicated. Existing text is
    preserved below the closing reference.
    """
    return runtime_pr_body(_context(state), current_body)


# ---------------------------------------------------------------- graph


def _route(next_node: str) -> Callable[[TaskState], str]:
    def route(state: TaskState) -> str:
        return "end" if state.get("status") == state_mod.FAILED else next_node

    return route


def build_graph(
    checkpointer: Any | None = None,
    on_node_start: Callable[[str, TaskState], None] | None = None,
    executor: Executor | None = None,
    workspace_manager: WorkspaceManager | None = None,
    destination: Destination | None = None,
    runtime=None,
):
    """Build the workflow graph.

    `on_node_start(node_name, state)` is invoked when each node starts (heartbeat).
    Nodes are guarded: any unhandled exception becomes a FAILED state instead of
    crashing the process.
    """

    def _guard(node_name: str, fn: Callable[[TaskState], dict[str, Any]]) -> Callable[[TaskState], dict[str, Any]]:
        def wrapped(state: TaskState) -> dict[str, Any]:
            if on_node_start is not None:
                try:
                    on_node_start(node_name, state)
                except Exception:
                    pass
            try:
                return fn(state)
            except Exception as exc:
                print(
                    f"[{_now()}] {node_name}: ERROR (unhandled) {type(exc).__name__}: {exc}",
                    flush=True,
                )
                return {"status": state_mod.FAILED, "error": f"unhandled exception in {node_name}: {exc}"}

        return wrapped

    builder = StateGraph(TaskState)
    workflow_runtime = runtime or compose_execution_runtime(
        executor=executor, workspace_manager=workspace_manager, destination=destination
    )
    nodes = {
        "prepare_workspace": lambda state: prepare_workspace(state, runtime=workflow_runtime),
        "plan": lambda state: plan(state, runtime=workflow_runtime),
        "implement": lambda state: implement(state, runtime=workflow_runtime),
        "test": lambda state: test(state, runtime=workflow_runtime),
        "create_pr": lambda state: create_pr(state, runtime=workflow_runtime),
        "cleanup": lambda state: cleanup(state, runtime=workflow_runtime),
    }
    for name, fn in nodes.items():
        builder.add_node(name, _guard(name, fn))

    builder.add_edge(START, "prepare_workspace")
    builder.add_conditional_edges("prepare_workspace", _route("plan"), {"plan": "plan", "end": END})
    builder.add_conditional_edges("plan", _route("implement"), {"implement": "implement", "end": END})
    builder.add_conditional_edges("implement", _route("test"), {"test": "test", "end": END})
    builder.add_conditional_edges("test", _route("create_pr"), {"create_pr": "create_pr", "end": END})
    builder.add_conditional_edges("create_pr", _route("cleanup"), {"cleanup": "cleanup", "end": END})
    builder.add_edge("cleanup", END)

    return builder.compile(checkpointer=checkpointer)
