"""Generic workflow builder: compile step specs into a linear LangGraph graph.

The builder wires arbitrary :class:`StepSpec` sequences through per-node
bookkeeping (timing, ``step_results`` entries, failure policy routing) without
knowing any concrete stage operation: node factories come from per-stage
catalogs such as :mod:`orchestrator.infra.langgraph.execution_steps`. The
builder produces no logs; progress printing stays inside node bodies.
"""

from __future__ import annotations

from time import monotonic
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from orchestrator.application.workflow import StepSpec
from orchestrator.infra.langgraph.state import FAILED, TaskState


NodeFn = Callable[[Any], dict[str, Any]]
NodeFactory = Callable[[StepSpec, Any], NodeFn]


def _route(next_node: str) -> Callable[[dict[str, Any]], str]:
    """Today's fail-fast router: stop on FAILED status, otherwise continue."""
    return lambda state: "end" if state.get("status") == FAILED else next_node


def _guarded(
    step: StepSpec,
    name: str,
    node_fn: NodeFn,
    on_node_start: Callable[[str, dict[str, Any]], None] | None,
) -> NodeFn:
    """Wrap one node with timing, bookkeeping, and the applied failure policy."""

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        t0 = monotonic()
        if on_node_start is not None:
            try:
                on_node_start(name, state)
            except Exception:
                pass
        failed = False
        try:
            update = dict(node_fn(state) or {})
        except Exception as exc:
            failed = True
            update = {"status": FAILED, "error": f"unhandled exception in {name}: {exc}"}
        elapsed = round(monotonic() - t0, 3)
        failed = failed or update.get("status") == FAILED
        extra = update.get("step_results", {}).get(name, {})
        entry = {"status": "failed" if failed else "succeeded",
                 "duration_s": elapsed, **extra}
        if failed:
            entry.setdefault("error", str(update.get("error") or "unknown failure"))
        update["step_results"] = {name: entry}
        if failed and step.on_failure == "continue":
            update.pop("status", None)  # failure is recorded, not fatal
            update.pop("error", None)
        return update

    return wrapped


def build_workflow(
    steps: list[StepSpec],
    factories: dict[str, NodeFactory],
    deps: Any,
    *,
    on_node_start: Callable[[str, dict[str, Any]], None] | None = None,
    state_schema: type[TaskState] = TaskState,
) -> CompiledStateGraph:
    """Compile ``START -> steps[0] -> ... -> steps[-1] -> END`` from step specs.

    ``factories`` maps each step type to a node factory receiving
    ``(step, deps)``; unknown step types, duplicate step names, and empty step
    lists fail fast at build time. ``deps`` is an opaque stage bundle passed
    through to every factory. ``on_node_start`` is invoked as
    ``(name, state)`` before every node (callback errors are swallowed).
    """
    if not steps:
        raise ValueError("workflow steps must define at least one step")
    names: list[str] = []
    seen: set[str] = set()
    for step in steps:
        if step.name in seen:
            raise ValueError(
                f"workflow step '{step.name}': duplicate step name (names must be unique)"
            )
        seen.add(step.name)
        if step.type not in factories:
            raise ValueError(
                f"workflow step '{step.name}': unknown step type '{step.type}' "
                "(no node factory registered)"
            )
        names.append(step.name)

    builder = StateGraph(state_schema)
    for step, name in zip(steps, names):
        node_fn = factories[step.type](step, deps)
        builder.add_node(name, _guarded(step, name, node_fn, on_node_start))
    builder.add_edge(START, names[0])
    for name, next_name in zip(names, names[1:]):
        builder.add_conditional_edges(name, _route(next_name), {next_name: next_name, "end": END})
    builder.add_edge(names[-1], END)
    return builder.compile()
