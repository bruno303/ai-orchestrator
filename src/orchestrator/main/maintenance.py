"""Retention sweep for expired workspaces, logs, and stale worktree registrations.

Successful tasks already remove their worktree immediately (see the execution
cleanup path). This module covers the residue that cannot be removed inline:

- workspaces kept for debugging after a failed or crashed run,
- per-task logs under ``ORCHESTRATOR_DATA_DIR/logs``,
- empty parent directories (e.g. ``WORKSPACES_DIR/discussion-``),
- stale ``git worktree`` registrations in the shared base clones.

Only paths strictly inside ``WORKSPACES_DIR`` and ``LOGS_DIR`` are ever
deleted; base clones (``ORCHESTRATOR_REPOS_DIR``) and ``data/state`` are never
removed. An entry is expired only when its workspace directory **and** its
event log are both older than the retention window, so an active run is never
swept.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.infra.filesystem import workspace as fs
from orchestrator.infra.git import client as git


@dataclass
class SweepReport:
    """Counters for one sweep pass."""

    enabled: bool = True
    workspaces_removed: list[str] = field(default_factory=list)
    logs_removed: list[str] = field(default_factory=list)
    worktrees_pruned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def removed_workspaces(self) -> int:
        return len(self.workspaces_removed)

    @property
    def removed_logs(self) -> int:
        return len(self.logs_removed)


def _is_expired(activity: float, cutoff: float) -> bool:
    return activity > 0.0 and activity < cutoff


def _safe_entries(root: Path) -> list[Path]:
    """List direct entries of ``root`` without following symlinks."""
    if not root.is_dir():
        return []
    return sorted(root.iterdir(), key=lambda path: path.name)


def _find_registered_repo(path: Path) -> Path | None:
    """Return the base clone that registers ``path`` as a worktree, if any."""
    if not git.REPOS_DIR.is_dir():
        return None
    for repo_dir in _safe_entries(git.REPOS_DIR):
        if not (repo_dir / ".git").exists():
            continue
        try:
            registered = git.list_worktrees(repo_dir)
        except git.GitError:
            continue
        if any(entry == path for entry in registered):
            return repo_dir
    return None


def _remove_workspace(path: Path, report: SweepReport) -> None:
    """Remove one expired workspace, preferring a registered worktree removal."""
    if path.is_symlink():
        # Never follow a symlink out of the workspace root; remove only the link.
        path.unlink()
        report.workspaces_removed.append(path.name)
        return
    repo_dir = _find_registered_repo(path)
    if repo_dir is not None:
        try:
            # Branch is intentionally empty: the sweeper cannot derive it from
            # the path, and stale local branches are recreated by ``worktree add -B``.
            git.remove_worktree(repo_dir, path, "")
        except git.GitError as exc:
            report.errors.append(f"{path.name}: {exc}")
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()
    if not path.exists() and not path.is_symlink():
        report.workspaces_removed.append(path.name)
        fs.remove_empty_parents(path, fs.WORKSPACES_DIR)
    elif path.exists():
        report.errors.append(f"{path.name}: workspace still exists after sweep")


def sweep_once(retention_days: int, now: float | None = None) -> SweepReport:
    """Sweep expired workspaces, task logs, and stale worktree registrations.

    ``retention_days < 1`` disables the sweep. Best effort: individual failures
    are collected in the report instead of aborting the pass.
    """
    report = SweepReport(enabled=retention_days >= 1)
    if not report.enabled:
        print("sweep: disabled (retention < 1 day)", flush=True)
        return report

    reference = time.time() if now is None else now
    cutoff = reference - retention_days * 24 * 60 * 60

    for entry in _safe_entries(fs.WORKSPACES_DIR):
        activity = fs.task_activity(entry.name, workspace_path=entry)
        if _is_expired(activity, cutoff):
            try:
                _remove_workspace(entry, report)
            except OSError as exc:
                report.errors.append(f"{entry.name}: {exc}")

    # Sweep log dirs by their own activity plus any matching workspace, so an
    # expired failed run loses both artifacts in the same pass. Container
    # directories (e.g. ``discussion-``) are skipped here because their token
    # never matches a real log dir; their emptiness is handled above.
    for entry in _safe_entries(fs.LOGS_DIR):
        workspace_path = fs.WORKSPACES_DIR / entry.name
        activity = fs.task_activity(entry.name, workspace_path=workspace_path)
        if _is_expired(activity, cutoff):
            try:
                fs.purge_task_logs(entry.name)
                report.logs_removed.append(entry.name)
            except OSError as exc:
                report.errors.append(f"{entry.name}: {exc}")

    # Drop registrations whose worktree directory is already gone.
    for repo_dir in _safe_entries(git.REPOS_DIR):
        if not (repo_dir / ".git").exists():
            continue
        try:
            registered = git.list_worktrees(repo_dir)
            git.prune_worktrees(repo_dir)
            remaining = git.list_worktrees(repo_dir)
            for path in registered:
                if path not in remaining:
                    report.worktrees_pruned.append(path.name)
        except git.GitError as exc:
            report.errors.append(f"{repo_dir.name}: {exc}")

    print(
        f"sweep: removed {report.removed_workspaces} workspaces, "
        f"{report.removed_logs} log dirs"
        + (f", {len(report.worktrees_pruned)} stale worktree registrations" if report.worktrees_pruned else "")
        + (f", {len(report.errors)} errors" if report.errors else ""),
        flush=True,
    )
    return report
