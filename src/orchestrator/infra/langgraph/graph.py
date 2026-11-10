"""LangGraph workflow for provider-neutral work-item execution.

Thin wrapper keeping today's public API: node bodies and state helpers moved
to :mod:`orchestrator.infra.langgraph.execution_steps`, graph assembly to
:mod:`orchestrator.infra.langgraph.builder`. ``build_graph`` composes the
built-in execution steps; the plan/implement prompt helpers and ``PLAN_FILE``
stay importable from this module (the historical import point).
"""

from __future__ import annotations

from typing import Callable

from orchestrator.infra.langgraph.state import TaskState
from orchestrator.infra.langgraph.builder import build_workflow
from orchestrator.infra.langgraph import execution_steps
from orchestrator.infra.langgraph.execution_steps import (
    ExecutionStageDeps,
    implement_prompt,  # noqa: F401 re-export
    plan_prompt,  # noqa: F401 re-export
)
from orchestrator.application.execution.service import (
    PLAN_FILE,  # noqa: F401 re-export
    ExecutionRuntime,
)
from orchestrator.application.workflow import default_steps


def build_graph(
    on_node_start: Callable[[str, TaskState], None] | None = None,
    *,
    runtime: ExecutionRuntime,
):
    deps = ExecutionStageDeps(runtime=runtime)
    graph = build_workflow(
        list(default_steps("execution")), execution_steps.STEP_FACTORIES, deps,
        on_node_start=on_node_start,
    )
    # Execution is deliberately process-local. GitHub publication markers are
    # the durable source of truth, not LangGraph checkpoints.
    return graph
