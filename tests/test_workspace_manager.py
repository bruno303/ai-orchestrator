"""Tests for the Git workspace provider adapter."""

from pathlib import Path

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
