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
from orchestrator.infra.langgraph.state import COMPLETED, FAILED, TaskState


NodeFn = Callable[[Any], dict[str, Any]]
NodeFactory = Callable[[StepSpec, Any], NodeFn]

# Reserved graph nodes: neither can collide with a valid step name (the slug
# pattern requires a leading lowercase letter).
_END_SENTINEL = "__end__"
_FINALIZE_NODE = "__finalize__"


def _route(next_node: str) -> Callable[[dict[str, Any]], str]:
    """Today's fail-fast router: stop on FAILED status, otherwise continue."""
    return lambda state: _END_SENTINEL if state.get("status") == FAILED else next_node


def _finalize(state: dict[str, Any]) -> dict[str, Any]:
    """Complete a run whose steps never set a terminal status."""
    if state.get("status") in (FAILED, COMPLETED):
        return {}
    return {"status": COMPLETED}


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
    state_schema: type[Any] = TaskState,
) -> CompiledStateGraph:
    """Compile ``START -> steps[0] -> ... -> steps[-1] -> __finalize__`` from step specs.

    ``factories`` maps each step type to a node factory receiving
    ``(step, deps)``; unknown step types, duplicate step names, and empty step
    lists fail fast at build time. ``deps`` is an opaque stage bundle passed
    through to every factory. ``on_node_start`` is invoked as
    ``(name, state)`` before every node (callback errors are swallowed); the
    unwrapped ``__finalize__`` node is not reported because it is not a step.

    Every step routes conditionally so a ``FAILED`` status aborts straight to
    ``END``; a run reaching the finalizer without a terminal status becomes
    ``COMPLETED``. The ``"__end__"`` sentinel is unreachable as a step name.
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
    builder.add_node(_FINALIZE_NODE, _finalize)
    for step, name in zip(steps, names):
        node_fn = factories[step.type](step, deps)
        builder.add_node(name, _guarded(step, name, node_fn, on_node_start))
    builder.add_edge(START, names[0])
    for name, next_name in zip(names, [*names[1:], _FINALIZE_NODE]):
        builder.add_conditional_edges(
            name, _route(next_name),
            {next_name: next_name, _END_SENTINEL: END},
        )
    builder.add_edge(_FINALIZE_NODE, END)
    return builder.compile()
