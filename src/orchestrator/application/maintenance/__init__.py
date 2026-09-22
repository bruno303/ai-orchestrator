"""Retention-based garbage collection for finished-task artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from orchestrator.application.ports import ArtifactStore


@dataclass(frozen=True)
class GCReport:
    """Summary of one garbage-collection pass."""

    logs_removed: tuple[str, ...] = ()
    workspaces_removed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    dry_run: bool = False


class MaintenanceApplication:
    """Reclaim artifacts that outlived their retention window.

    A finished task keeps its logs and events for ``retention_days`` so failures
    can be investigated, while its workspace, worktree, and local branch are
    removed as soon as the task ends. This pass deletes whatever survived its
    window (for example after a crash between publication and cleanup) without
    touching recent activity. It never raises: per-item failures are reported
    and retried on the next pass.
    """

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        retention_days: int = 7,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.retention_days = retention_days
        self.now = now or (lambda: datetime.now(timezone.utc))

    def collect(self, *, dry_run: bool = False) -> GCReport:
        cutoff = self.now() - timedelta(days=self.retention_days)
        logs: list[str] = []
        workspaces: list[str] = []
        errors: list[str] = []
        try:
            expired = list(self.artifacts.expired_tasks(cutoff))
        except Exception as exc:
            expired = []
            errors.append(f"expired tasks: {exc}")
        for task_id in expired:
            if dry_run:
                logs.append(task_id)
                continue
            try:
                self.artifacts.remove_task_logs(task_id)
                logs.append(task_id)
            except Exception as exc:
                errors.append(f"logs {task_id}: {exc}")
        try:
            stale = list(self.artifacts.stale_workspaces(cutoff))
        except Exception as exc:
            stale = []
            errors.append(f"stale workspaces: {exc}")
        for path in stale:
            if dry_run:
                workspaces.append(path)
                continue
            try:
                self.artifacts.remove_workspace(path)
                workspaces.append(path)
            except Exception as exc:
                errors.append(f"workspace {path}: {exc}")
        if not dry_run:
            try:
                self.artifacts.prune_base_clones()
            except Exception as exc:
                errors.append(f"base clones: {exc}")
        return GCReport(tuple(logs), tuple(workspaces), tuple(errors), dry_run)
