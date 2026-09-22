"""Tests for the Git workspace provider adapter."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

import pytest

from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git
from orchestrator.domain import Context
from orchestrator.infra.git.workspace import GitWorkspaceManager
from orchestrator.application.ports import WorkspaceRequest
from orchestrator.infra.github import auth as github_auth


def test_prepare_and_cleanup_use_existing_git_operations(remote_repo, monkeypatch, tmp_path):
    calls: list[str] = []
    original_prepare = git.create_worktree
    original_cleanup = git.remove_worktree

    def create(repo, path, branch, base):
        calls.append("create")
        original_prepare(repo, path, branch, base)

    def remove(repo, path, branch):
        calls.append("remove")
        original_cleanup(repo, path, branch)

    monkeypatch.setattr(git, "create_worktree", create)
    monkeypatch.setattr(git, "remove_worktree", remove)
    workspace_path = tmp_path / "workspace"
    result = GitWorkspaceManager().prepare(
        WorkspaceRequest(
            "company/backend#1", "company/backend", "ai/issue-1", "main",
            context=Context({"git": {"repository_url": f"file://{remote_repo}", "workspace": str(workspace_path)}}),
        )
    )
    assert Path(result.workspace).exists()
    GitWorkspaceManager().cleanup(result)
    assert calls == ["create", "remove"]
    assert not workspace_path.exists()


def test_cleanup_removes_generated_artifacts_and_local_branch(remote_repo, tmp_path):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    result = manager.prepare(
        WorkspaceRequest(
            "company/backend#cleanup", "company/backend", "ai/cleanup", "main",
            context=Context({"git": {"repository_url": f"file://{remote_repo}", "workspace": str(workspace_path)}}),
        )
    )
    plan = workspace_path / ".agents/plans/plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("temporary plan\n")

    manager.cleanup(result)

    assert not plan.exists()
    assert not workspace_path.exists()
    assert not git._run(["git", "branch", "--list", "ai/cleanup"], cwd=Path(result.context.namespace("git")["repo_dir"]), check=False).stdout.strip()


def test_prepare_derives_branch_and_workspace_from_task_id(remote_repo):
    manager = GitWorkspaceManager()
    result = manager.prepare(WorkspaceRequest(
        "company/backend#7", "company/backend", "", "main",
        context=Context({"git": {"repository_url": f"file://{remote_repo}"}}),
    ))

    assert result.branch == "ai/company-backend-7"
    assert Path(result.workspace) == workspace.task_workspace("company/backend#7")
    assert Path(result.workspace).exists()
    manager.cleanup(result)


def test_prepare_recreates_existing_execution_workspace(remote_repo, tmp_path):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    request = WorkspaceRequest(
        "company/backend#8", "company/backend", "ai/issue-8", "main",
        workspace=str(workspace_path),
        context=Context({"git": {"repository_url": f"file://{remote_repo}"}}),
    )

    first = manager.prepare(request)
    sentinel = workspace_path / "dirty.txt"
    sentinel.write_text("stale workspace\n")

    second = manager.prepare(request)

    assert second.workspace == first.workspace
    assert not sentinel.exists()
    assert workspace_path.exists()
    assert (workspace_path / ".git").exists()
    manager.cleanup(second)
    assert not workspace_path.exists()


def test_prepare_reuses_existing_execution_workspace_when_requested(remote_repo, tmp_path):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    request = WorkspaceRequest(
        "company/backend#8-retry", "company/backend", "ai/issue-8", "main",
        workspace=str(workspace_path),
        context=Context({"git": {"repository_url": f"file://{remote_repo}"}}),
    )

    first = manager.prepare(request)
    sentinel = workspace_path / "unfinished.txt"
    sentinel.write_text("preserve this work\n")

    second = manager.prepare(
        WorkspaceRequest(
            "company/backend#8-retry", "company/backend", "ai/issue-8", "main",
            workspace=str(workspace_path), reuse_workspace=True,
            context=Context({"git": {"repository_url": f"file://{remote_repo}"}}),
        )
    )

    assert second.workspace == first.workspace
    assert sentinel.read_text() == "preserve this work\n"
    manager.cleanup(second)
    assert not workspace_path.exists()


def test_prepare_removes_existing_plain_directory(remote_repo, tmp_path):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "stale.txt").write_text("stale workspace\n")

    result = manager.prepare(WorkspaceRequest(
        "company/backend#9", "company/backend", "ai/issue-9", "main",
        workspace=str(workspace_path),
        context=Context({"git": {"repository_url": f"file://{remote_repo}"}}),
    ))

    assert result.workspace == str(workspace_path)
    assert not (workspace_path / "stale.txt").exists()
    assert (workspace_path / ".git").exists()
    manager.cleanup(result)


def test_prepare_requires_explicit_repository_url(monkeypatch):
    cloned = False

    def ensure_base_clone(*args):
        nonlocal cloned
        cloned = True

    monkeypatch.setattr(git, "ensure_base_clone", ensure_base_clone)

    with pytest.raises(git.GitError, match="requires a repository URL"):
        GitWorkspaceManager().prepare(WorkspaceRequest(
            "company/backend#1", "company/backend", "ai/issue-1", "main",
            context=Context({"git": {"workspace": "/tmp/workspace"}}),
        ))

    assert not cloned


def test_prune_expired_completed_task_logs(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(workspace, "LOGS_DIR", logs)
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    old = logs / "old-task"
    old.mkdir()
    (old / "events.jsonl").write_text(json.dumps({
        "ts": (now - timedelta(days=8)).isoformat(),
        "event": "task_end",
        "status": "COMPLETED",
    }) + "\n")
    (old / "plan.log").write_text("temporary output\n")

    recent = logs / "recent-task"
    recent.mkdir()
    (recent / "events.jsonl").write_text(json.dumps({
        "ts": (now - timedelta(days=6)).isoformat(),
        "event": "task_end",
        "status": "COMPLETED",
    }) + "\n")

    failed = logs / "failed-task"
    failed.mkdir()
    (failed / "events.jsonl").write_text(json.dumps({
        "ts": (now - timedelta(days=8)).isoformat(),
        "event": "task_end",
        "status": "FAILED",
    }) + "\n")

    assert workspace.prune_expired_task_logs(now) == 1
    assert not old.exists()
    assert recent.exists()
    assert failed.exists()


def test_workspace_manager_uses_its_injected_identity_bound_git_client(tmp_path):
    calls = []

    class FakeGitClient:
        identity = github_auth.GitHubIdentity("user")

        def ensure_base_clone(self, repository, url):
            calls.append(("clone", repository, url, self.identity.mode))
            return tmp_path / "repo"

        def create_worktree(self, repo, path, branch, base):
            calls.append(("worktree", branch, self.identity.mode))

    manager = GitWorkspaceManager(git_client=FakeGitClient())
    manager.prepare(WorkspaceRequest(
        "company/backend#1", "company/backend", "ai/issue-1", "main",
        workspace=str(tmp_path / "workspace"),
        context=Context({"git": {"repository_url": "https://github.com/company/backend.git"}}),
    ))

    assert calls == [
        ("clone", "company/backend", "https://github.com/company/backend.git", "user"),
        ("worktree", "ai/issue-1", "user"),
    ]


def test_review_prepare_fetches_fork_commit_and_falls_back_to_origin(monkeypatch, tmp_path):
    fetched = []
    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: tmp_path / "repo")
    monkeypatch.setattr(git, "fetch_commit", lambda repo, commit, remote: fetched.append((commit, remote)))
    monkeypatch.setattr(git, "create_detached_worktree", lambda repo, path, commit: None)
    manager = GitWorkspaceManager()
    result = manager.prepare(WorkspaceRequest(
        "review:company/backend#4", "company/backend", "", "main",
        purpose="review", revision="fork-sha", workspace=str(tmp_path / "ws"),
        context=Context({"git": {"repository_url": "origin-url"}}),
    ))
    assert fetched == [("fork-sha", "origin")]
    assert result.branch == ""


def test_pr_execution_fetches_head_sha_from_fork_and_attaches_branch(monkeypatch, tmp_path):
    calls = []

    class FakeGit:
        def ensure_base_clone(self, repository, url):
            calls.append(("clone", repository, url))
            return tmp_path / "repo"

        def fetch_commit(self, repo, commit, remote):
            calls.append(("fetch", commit, remote))

        def create_worktree(self, repo, path, branch, base, **kwargs):
            calls.append(("worktree", branch, base, kwargs["start_point"]))

    result = GitWorkspaceManager(git_client=FakeGit()).prepare(WorkspaceRequest(
        "owner/repo#pr-4", "owner/repo", "topic", "main",
        workspace=str(tmp_path / "ws"),
        context=Context({"git": {
            "repository_url": "https://github.com/fork/repo.git",
            "base_repository_url": "https://github.com/owner/repo.git",
            "fetch_url": "https://github.com/fork/repo.git",
            "revision": "head-sha",
        }}),
    ))
    assert calls == [
        ("clone", "owner/repo", "https://github.com/owner/repo.git"),
        ("fetch", "head-sha", "https://github.com/fork/repo.git"),
        ("worktree", "topic", "main", "head-sha"),
    ]
    assert result.branch == "topic"


def test_review_prepare_propagates_unavailable_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: tmp_path / "repo")
    monkeypatch.setattr(git, "fetch_commit", lambda *args: (_ for _ in ()).throw(git.GitError("unknown commit")))
    with pytest.raises(git.GitError, match="unknown commit"):
        GitWorkspaceManager().prepare(WorkspaceRequest(
            "review:company/backend#4", "company/backend", "", "main",
            purpose="review", revision="missing", workspace=str(tmp_path / "ws"),
            context=Context({"git": {"repository_url": "origin-url"}}),
        ))
