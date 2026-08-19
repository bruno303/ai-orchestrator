"""Reusable runtime services shared by workflow adapters."""

from __future__ import annotations

from typing import Any

from orchestrator.providers import Destination, Executor, WorkspaceManager
from orchestrator.runtime.execution import ExecutionRuntime


def compose_execution_runtime(
    *,
    executor: Executor | None = None,
    workspace_manager: WorkspaceManager | None = None,
    destination: Destination | None = None,
) -> ExecutionRuntime:
    """Compose the issue workflow runtime from provider implementations."""
    if executor is None:
        from orchestrator.opencode import OpenCodeExecutor

        executor = OpenCodeExecutor()
    if workspace_manager is None:
        from orchestrator.git_workspace import GitWorkspaceManager

        workspace_manager = GitWorkspaceManager()
    if destination is None:
        from orchestrator.github_destination import GitHubDestination

        destination = GitHubDestination()
    return ExecutionRuntime(executor, workspace_manager, destination)


__all__ = ["ExecutionRuntime", "compose_execution_runtime"]
