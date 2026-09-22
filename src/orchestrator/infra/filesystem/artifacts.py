"""Retention-based cleanup of task logs, workspaces, and clone residue."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git


def _last_activity(directory: Path) -> datetime:
    """Return the newest modification time of an artifact directory."""
    newest = directory.stat().st_mtime
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(newest, timezone.utc)


class FilesystemArtifactStore:
    """Remove finished-task artifacts that outlived the retention window."""

    def __init__(self, git_client=None) -> None:
        self.git_client = git_client or git.GitClient()

    def expired_tasks(self, cutoff: datetime) -> list[str]:
        """Return task tokens whose logs and events are older than the cutoff."""
        if not workspace.LOGS_DIR.exists():
            return []
        return sorted(
            path.name
            for path in workspace.LOGS_DIR.iterdir()
            if path.is_dir() and _last_activity(path) < cutoff
        )

    def remove_task_logs(self, task_id: str) -> None:
        shutil.rmtree(workspace.task_logs_dir(task_id), ignore_errors=True)

    def stale_workspaces(self, cutoff: datetime) -> list[str]:
        """Return workspace paths with no activity since the cutoff."""
        return [
            str(path)
            for path in self._workspace_units()
            if _last_activity(path) < cutoff
        ]

    def remove_workspace(self, path: str) -> None:
        """Remove one workspace folder together with its worktree registration."""
        target = Path(path)
        for repo_dir in self._base_clones():
            self.git_client.remove_worktree(repo_dir, target, "")
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        workspace.remove_empty_parent(target)

    def prune_base_clones(self) -> None:
        """Drop stale worktree metadata and already-pushed branches from clones."""
        for repo_dir in self._base_clones():
            self.git_client.prune_base_clone(repo_dir)

    def _workspace_units(self) -> list[Path]:
        root = workspace.WORKSPACES_DIR
        if not root.exists():
            return []
        units: list[Path] = []
        for path in sorted(root.iterdir()):
            if path.is_dir() and path.name == workspace.DISCUSSIONS_DIR_NAME:
                units.extend(sorted(path.iterdir()))
            else:
                units.append(path)
        return [path for path in units if path.is_dir() or path.is_symlink()]

    def _base_clones(self) -> list[Path]:
        if not git.REPOS_DIR.exists():
            return []
        return sorted(
            path for path in git.REPOS_DIR.iterdir()
            if path.is_dir() and (path / ".git").exists()
        )
