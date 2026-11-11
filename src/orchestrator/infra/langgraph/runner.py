"""Shared streaming runner for compiled stage workflows.

All stages use the same in-memory execution contract: stream node updates,
print one terminal line per node, and append ``node_start``/``node_end`` events
to the task event log. Stage-specific graph assembly stays in the catalogs and
the composition root; this module only knows about the generic builder.
"""

from __future__ import annotations

import time
from time import monotonic
from typing import Any, Callable

from orchestrator.application.workflow import StepSpec
from orchestrator.infra.filesystem import workspace
from orchestrator.infra.langgraph import state as state_mod
from orchestrator.infra.langgraph.builder import NodeFactory, build_workflow


def _now() -> str:
    return time.strftime("%H:%M:%S")


def run_workflow_graph(
    seed: dict[str, Any],
    task_id: str,
    *,
    steps: list[StepSpec],
    deps: Any,
    factories: dict[str, NodeFactory],
    state_schema: type = state_mod.TaskState,
    on_node_start: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one linear workflow and retain the merged in-memory state.

    Every node produces a start event and exactly one terminal event: a
    state-level status, an error, or (for generic ``command``/``agent`` steps
    and continued failures) its ``step_results`` entry.
    """
    starts: dict[str, float] = {}

    def start(node: str, state: dict[str, Any]) -> None:
        starts[node] = monotonic()
        workspace.append_event(task_id, event="node_start", node=node)
        if on_node_start is not None:
            try:
                on_node_start(node, state)
            except Exception:
                pass

    graph = build_workflow(
        steps, factories, deps, on_node_start=start, state_schema=state_schema,
    )
    # Stream updates are partial; retain the input seed while merging the
    # in-memory execution result instead of querying durable graph state.
    result: dict[str, Any] = dict(seed)
    for chunk in graph.stream(seed, stream_mode="updates"):
        for node, update in chunk.items():
            # ``step_results`` uses a merge reducer in the graph state; stream
            # updates are node-local, so merge it here as well instead of
            # letting the latest node replace earlier entries.
            if "step_results" in update:
                merged = dict(result.get("step_results") or {})
                merged.update(update["step_results"])
                update = {**update, "step_results": merged}
            result.update(update)
            status, error = update.get("status"), update.get("error")
            duration = round(monotonic() - starts.get(node, monotonic()), 1)
            if status:
                print(f"[{_now()}] [{node}] {status} ({duration}s)", flush=True)
                workspace.append_event(
                    task_id, event="node_end", node=node, status=status,
                    duration_s=duration,
                )
            elif error:
                print(f"[{_now()}] [{node}] error: {error}", flush=True)
                workspace.append_event(
                    task_id, event="node_end", node=node,
                    status=state_mod.FAILED, duration_s=duration,
                    error=str(error)[:200],
                )
            else:
                entry = (update.get("step_results") or {}).get(node)
                if isinstance(entry, dict) and entry.get("status"):
                    duration = round(float(entry.get("duration_s") or duration), 1)
                    if entry["status"] == "failed":
                        failure = str(entry.get("error") or "unknown failure")
                        print(f"[{_now()}] [{node}] error: {failure}", flush=True)
                        workspace.append_event(
                            task_id, event="node_end", node=node,
                            status=entry["status"], duration_s=duration,
                            error=failure[:200],
                        )
                    else:
                        print(f"[{_now()}] [{node}] {entry['status']} ({duration}s)", flush=True)
                        workspace.append_event(
                            task_id, event="node_end", node=node,
                            status=entry["status"], duration_s=duration,
                        )
    return result
