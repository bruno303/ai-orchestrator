"""Tests for the Git workspace provider adapter."""

from pathlib import Path

import pytest

from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git
from orchestrator.domain import Context
from orchestrator.infra.git.workspace import GitWorkspaceManager
from orchestrator.application.ports import WorkspaceRequest


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


def test_review_prepare_propagates_unavailable_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: tmp_path / "repo")
    monkeypatch.setattr(git, "fetch_commit", lambda *args: (_ for _ in ()).throw(git.GitError("unknown commit")))
    with pytest.raises(git.GitError, match="unknown commit"):
        GitWorkspaceManager().prepare(WorkspaceRequest(
            "review:company/backend#4", "company/backend", "", "main",
            purpose="review", revision="missing", workspace=str(tmp_path / "ws"),
            context=Context({"git": {"repository_url": "origin-url"}}),
        ))
