"""Tests for the Git workspace provider adapter."""

from pathlib import Path

import pytest

from orchestrator import git
from orchestrator.git_workspace import GitWorkspaceManager
from orchestrator.providers import WorkspaceRequest


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
            {"repository_url": f"file://{remote_repo}", "workspace": str(workspace_path)},
        )
    )
    assert Path(result.workspace).exists()
    GitWorkspaceManager().cleanup(result)
    assert calls == ["create", "remove"]
    assert not workspace_path.exists()


def test_review_prepare_fetches_fork_commit_and_falls_back_to_origin(monkeypatch, tmp_path):
    fetched = []
    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: tmp_path / "repo")
    monkeypatch.setattr(git, "fetch_commit", lambda repo, commit, remote: fetched.append((commit, remote)))
    monkeypatch.setattr(git, "create_detached_worktree", lambda repo, path, commit: None)
    monkeypatch.setattr("orchestrator.github.get_clone_url", lambda repository: "origin-url")
    monkeypatch.setattr("orchestrator.github.get_default_branch", lambda repository: "main")
    manager = GitWorkspaceManager()
    manager.prepare(WorkspaceRequest(
        "review:company/backend#4", "company/backend", "", "main",
        {"head_sha": "fork-sha", "head_clone_url": "", "workspace": str(tmp_path / "ws")},
    ))
    assert fetched == [("fork-sha", "origin")]


def test_review_prepare_propagates_unavailable_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: tmp_path / "repo")
    monkeypatch.setattr(git, "fetch_commit", lambda *args: (_ for _ in ()).throw(git.GitError("unknown commit")))
    monkeypatch.setattr("orchestrator.github.get_clone_url", lambda repository: "origin-url")
    monkeypatch.setattr("orchestrator.github.get_default_branch", lambda repository: "main")
    with pytest.raises(git.GitError, match="unknown commit"):
        GitWorkspaceManager().prepare(WorkspaceRequest(
            "review:company/backend#4", "company/backend", "", "main",
            {"head_sha": "missing", "workspace": str(tmp_path / "ws")},
        ))
