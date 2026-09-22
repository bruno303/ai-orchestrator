"""Tests for retention-based artifact garbage collection."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.application.maintenance import GCReport, MaintenanceApplication
from orchestrator.application.ports import WorkspaceRequest
from orchestrator.domain import Context
from orchestrator.infra.filesystem import artifacts, workspace
from orchestrator.infra.git import client as git
from orchestrator.infra.git.workspace import GitWorkspaceManager


NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, expired=(), stale=(), failing=()):
        self.expired = list(expired)
        self.stale = list(stale)
        self.failing = set(failing)
        self.removed_logs: list[str] = []
        self.removed_workspaces: list[str] = []
        self.pruned = 0
        self.cutoffs: list[datetime] = []

    def expired_tasks(self, cutoff):
        self.cutoffs.append(cutoff)
        return list(self.expired)

    def remove_task_logs(self, task_id):
        if task_id in self.failing:
            raise RuntimeError("logs unavailable")
        self.removed_logs.append(task_id)

    def stale_workspaces(self, cutoff):
        return list(self.stale)

    def remove_workspace(self, path):
        self.removed_workspaces.append(path)

    def prune_base_clones(self):
        self.pruned += 1


def _age(path: Path, days: float) -> None:
    """Backdate a directory tree so it is older than the retention window."""
    stamp = time.time() - days * 86400
    for item in (path, *path.rglob("*")):
        os.utime(item, (stamp, stamp))


@pytest.fixture
def artifact_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(git, "REPOS_DIR", tmp_path / "repos")
    return tmp_path


def test_collect_removes_expired_logs_and_stale_workspaces():
    store = FakeStore(expired=["owner-repo-1"], stale=["/tmp/ws-1"])

    report = MaintenanceApplication(store, retention_days=7, now=lambda: NOW).collect()

    assert store.cutoffs == [NOW - timedelta(days=7)]
    assert store.removed_logs == ["owner-repo-1"]
    assert store.removed_workspaces == ["/tmp/ws-1"]
    assert store.pruned == 1
    assert report == GCReport(("owner-repo-1",), ("/tmp/ws-1",), (), False)


def test_collect_dry_run_lists_without_deleting():
    store = FakeStore(expired=["owner-repo-1"], stale=["/tmp/ws-1"])

    report = MaintenanceApplication(store, now=lambda: NOW).collect(dry_run=True)

    assert report.dry_run is True
    assert report.logs_removed == ("owner-repo-1",)
    assert report.workspaces_removed == ("/tmp/ws-1",)
    assert store.removed_logs == []
    assert store.removed_workspaces == []
    assert store.pruned == 0


def test_collect_reports_errors_without_raising():
    store = FakeStore(expired=["owner-repo-1", "owner-repo-2"], failing={"owner-repo-2"})

    report = MaintenanceApplication(store, now=lambda: NOW).collect()

    assert report.logs_removed == ("owner-repo-1",)
    assert report.errors == ("logs owner-repo-2: logs unavailable",)


def test_collect_reports_listing_failures():
    class Broken(FakeStore):
        def expired_tasks(self, cutoff):
            raise RuntimeError("logs unavailable")

    report = MaintenanceApplication(Broken(), now=lambda: NOW).collect()

    assert report.errors == ("expired tasks: logs unavailable",)
    assert report.logs_removed == ()


def test_filesystem_store_expires_task_logs_after_the_retention_window(artifact_dirs):
    store = artifacts.FilesystemArtifactStore()
    old, fresh = workspace.LOGS_DIR / "owner-repo-1", workspace.LOGS_DIR / "owner-repo-2"
    for path in (old, fresh):
        path.mkdir(parents=True)
        (path / "events.jsonl").write_text("{}\n")
    _age(old, 8)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    assert store.expired_tasks(cutoff) == ["owner-repo-1"]

    store.remove_task_logs("owner-repo-1")

    assert not old.exists()
    assert fresh.exists()


def test_filesystem_store_keeps_task_logs_inside_the_window(artifact_dirs):
    store = artifacts.FilesystemArtifactStore()
    recent = workspace.LOGS_DIR / "owner-repo-1"
    recent.mkdir(parents=True)
    (recent / "events.jsonl").write_text("{}\n")
    _age(recent, 3)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    assert store.expired_tasks(cutoff) == []


def test_filesystem_store_removes_stale_workspaces_and_their_group_folder(artifact_dirs):
    store = artifacts.FilesystemArtifactStore()
    stale = workspace.discussion_group_dir() / "owner-repo-3"
    live = workspace.WORKSPACES_DIR / "owner-repo-4"
    for path in (stale, live):
        path.mkdir(parents=True)
        (path / "file.txt").write_text("x\n")
    _age(stale, 8)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    assert store.stale_workspaces(cutoff) == [str(stale)]

    store.remove_workspace(str(stale))

    assert not stale.exists()
    assert not workspace.discussion_group_dir().exists()
    assert live.exists()


def test_filesystem_store_removes_a_crashed_worktree_from_the_base_clone(remote_repo):
    store = artifacts.FilesystemArtifactStore()
    result = GitWorkspaceManager().prepare(WorkspaceRequest(
        "company/backend#14", "company/backend", "ai/issue-14", "main",
        context=Context({"git": {"repository_url": f"file://{remote_repo}"}}),
    ))
    crashed = Path(result.workspace)
    repo_dir = Path(result.context.namespace("git")["repo_dir"])
    try:
        _age(crashed, 8)  # the process died before cleanup could run

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.stale_workspaces(cutoff) == [str(crashed)]

        store.remove_workspace(str(crashed))

        assert not crashed.exists()
        assert all(
            path.resolve() != crashed.resolve() for path in git.list_worktrees(repo_dir)
        )
    finally:
        git.remove_branch(repo_dir, "ai/issue-14")


def test_prune_base_clones_prunes_every_base_clone(artifact_dirs):
    clone = git.REPOS_DIR / "company-backend"
    (clone / ".git").mkdir(parents=True)
    calls: list[Path] = []

    class FakeGit:
        def prune_base_clone(self, repo_dir):
            calls.append(repo_dir)

    artifacts.FilesystemArtifactStore(git_client=FakeGit()).prune_base_clones()

    assert calls == [clone]
