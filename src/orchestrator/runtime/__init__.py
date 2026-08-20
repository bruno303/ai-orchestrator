"""Reusable runtime services shared by workflow adapters."""

from __future__ import annotations

from orchestrator import config
from orchestrator.providers import (
    DESTINATION_PROVIDERS,
    EXECUTOR_PROVIDERS,
    WORKSPACE_PROVIDERS,
    Destination,
    Executor,
    WorkspaceManager,
)
from orchestrator.runtime.execution import ExecutionRuntime


def compose_execution_runtime(
    *,
    executor: Executor | None = None,
    workspace_manager: WorkspaceManager | None = None,
    destination: Destination | None = None,
) -> ExecutionRuntime:
    """Compose the issue workflow runtime from provider implementations."""
    pipeline = config.load_pipeline_config()
    if executor is None:
        executor = EXECUTOR_PROVIDERS.create(
            pipeline.executor.type, {**pipeline.executor.options, "_runtime": True}
        )
    if workspace_manager is None:
        workspace_manager = WORKSPACE_PROVIDERS.create(
            pipeline.workspace_manager.type, {**pipeline.workspace_manager.options, "_runtime": True}
        )
    if destination is None:
        destination = DESTINATION_PROVIDERS.create(
            pipeline.destination.type, {**pipeline.destination.options, "_runtime": True}
        )
    return ExecutionRuntime(executor, workspace_manager, destination)


__all__ = ["ExecutionRuntime", "compose_execution_runtime"]
