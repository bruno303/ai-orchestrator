"""Tests for the Git workspace provider adapter."""

from pathlib import Path

import pytest

from orchestrator.application.ports import WorkspaceRequest
from orchestrator.domain import Context
from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git
from orchestrator.infra.git.workspace import GitWorkspaceManager
from orchestrator.infra.github import auth as github_auth


def test_prepare_and_cleanup_create_self_contained_workspace(remote_repo, tmp_path):
    workspace_path = tmp_path / "workspace"
    result = GitWorkspaceManager().prepare(
        WorkspaceRequest(
            "company/backend#1",
            "company/backend",
            "ai/issue-1",
            "main",
            context=Context(
                {
                    "git": {
                        "repository_url": f"file://{remote_repo}",
                        "workspace": str(workspace_path),
                    }
                }
            ),
        )
    )

    assert Path(result.workspace) == workspace_path
    assert (workspace_path / ".git").is_dir()
    assert not (workspace_path / ".git/objects/info/alternates").exists()

    GitWorkspaceManager().cleanup(result)

    assert not workspace_path.exists()


def test_prepare_derives_branch_and_workspace_from_task_id(remote_repo):
    manager = GitWorkspaceManager()
    result = manager.prepare(
        WorkspaceRequest(
            "company/backend#7",
            "company/backend",
            "",
            "main",
            context=Context(
                {"git": {"repository_url": f"file://{remote_repo}"}}
            ),
        )
    )

    assert result.branch == "ai/company-backend-7"
    assert Path(result.workspace) == workspace.task_workspace("company/backend#7")
    assert (Path(result.workspace) / ".git").is_dir()
    manager.cleanup(result)


def test_prepare_recreates_existing_execution_workspace(remote_repo, tmp_path):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    request = WorkspaceRequest(
        "company/backend#8",
        "company/backend",
        "ai/issue-8",
        "main",
        workspace=str(workspace_path),
        context=Context(
            {"git": {"repository_url": f"file://{remote_repo}"}}
        ),
    )

    first = manager.prepare(request)
    sentinel = workspace_path / "dirty.txt"
    sentinel.write_text("stale workspace\n")

    second = manager.prepare(request)

    assert second.workspace == first.workspace
    assert not sentinel.exists()
    assert (workspace_path / ".git").is_dir()
    manager.cleanup(second)


def test_prepare_reuses_existing_execution_workspace_when_requested(
    remote_repo, tmp_path
):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    request = WorkspaceRequest(
        "company/backend#8-retry",
        "company/backend",
        "ai/issue-8",
        "main",
        workspace=str(workspace_path),
        context=Context(
            {"git": {"repository_url": f"file://{remote_repo}"}}
        ),
    )

    first = manager.prepare(request)
    sentinel = workspace_path / "unfinished.txt"
    sentinel.write_text("preserve this work\n")

    second = manager.prepare(
        WorkspaceRequest(
            "company/backend#8-retry",
            "company/backend",
            "ai/issue-8",
            "main",
            workspace=str(workspace_path),
            reuse_workspace=True,
            context=Context(
                {"git": {"repository_url": f"file://{remote_repo}"}}
            ),
        )
    )

    assert second.workspace == first.workspace
    assert sentinel.read_text() == "preserve this work\n"
    manager.cleanup(second)


def test_prepare_replaces_legacy_linked_worktree_when_reuse_requested(
    remote_repo, tmp_path
):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / ".git").write_text(
        "gitdir: /outside/base/.git/worktrees/legacy\n"
    )
    (workspace_path / "stale.txt").write_text("legacy\n")

    result = GitWorkspaceManager().prepare(
        WorkspaceRequest(
            "company/backend#legacy",
            "company/backend",
            "ai/legacy",
            "main",
            workspace=str(workspace_path),
            reuse_workspace=True,
            context=Context(
                {"git": {"repository_url": f"file://{remote_repo}"}}
            ),
        )
    )

    assert (workspace_path / ".git").is_dir()
    assert not (workspace_path / "stale.txt").exists()
    GitWorkspaceManager().cleanup(result)


def test_prepare_removes_existing_plain_directory(remote_repo, tmp_path):
    manager = GitWorkspaceManager()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "stale.txt").write_text("stale workspace\n")

    result = manager.prepare(
        WorkspaceRequest(
            "company/backend#9",
            "company/backend",
            "ai/issue-9",
            "main",
            workspace=str(workspace_path),
            context=Context(
                {"git": {"repository_url": f"file://{remote_repo}"}}
            ),
        )
    )

    assert result.workspace == str(workspace_path)
    assert not (workspace_path / "stale.txt").exists()
    assert (workspace_path / ".git").is_dir()
    manager.cleanup(result)


def test_prepare_requires_explicit_repository_url(monkeypatch):
    cloned = False

    def ensure_base_clone(*args):
        nonlocal cloned
        cloned = True

    monkeypatch.setattr(git, "ensure_base_clone", ensure_base_clone)

    with pytest.raises(git.GitError, match="requires a repository URL"):
        GitWorkspaceManager().prepare(
            WorkspaceRequest(
                "company/backend#1",
                "company/backend",
                "ai/issue-1",
                "main",
                context=Context({"git": {"workspace": "/tmp/workspace"}}),
            )
        )

    assert not cloned


def test_workspace_manager_uses_its_injected_identity_bound_git_client(tmp_path):
    calls = []

    class FakeGitClient:
        identity = github_auth.GitHubIdentity("user")

        def ensure_base_clone(self, repository, url):
            calls.append(("cache", repository, url, self.identity.mode))
            return tmp_path / "repo"

        def clone_workspace(self, repo, path, url):
            calls.append(("workspace", repo, path, url, self.identity.mode))

        def checkout_branch(self, path, branch, base, start_point=None):
            calls.append(
                ("checkout", branch, base, start_point, self.identity.mode)
            )

    manager = GitWorkspaceManager(git_client=FakeGitClient())
    manager.prepare(
        WorkspaceRequest(
            "company/backend#1",
            "company/backend",
            "ai/issue-1",
            "main",
            workspace=str(tmp_path / "workspace"),
            context=Context(
                {
                    "git": {
                        "repository_url": "https://github.com/company/backend.git"
                    }
                }
            ),
        )
    )

    assert calls == [
        (
            "cache",
            "company/backend",
            "https://github.com/company/backend.git",
            "user",
        ),
        (
            "workspace",
            tmp_path / "repo",
            tmp_path / "workspace",
            "https://github.com/company/backend.git",
            "user",
        ),
        ("checkout", "ai/issue-1", "main", None, "user"),
    ]


def test_review_prepare_fetches_fork_commit_in_workspace(monkeypatch, tmp_path):
    calls = []
    repo = tmp_path / "repo"
    ws = tmp_path / "ws"

    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: repo)
    monkeypatch.setattr(
        git,
        "clone_workspace",
        lambda source, path, url: calls.append(
            ("workspace", source, path, url)
        ),
    )
    monkeypatch.setattr(
        git,
        "fetch_commit",
        lambda path, commit, remote: calls.append(
            ("fetch", path, commit, remote)
        ),
    )
    monkeypatch.setattr(
        git,
        "checkout_detached",
        lambda path, commit: calls.append(("detached", path, commit)),
    )

    result = GitWorkspaceManager().prepare(
        WorkspaceRequest(
            "review:company/backend#4",
            "company/backend",
            "",
            "main",
            purpose="review",
            revision="fork-sha",
            workspace=str(ws),
            context=Context({"git": {"repository_url": "origin-url"}}),
        )
    )

    assert calls == [
        ("workspace", repo, ws, "origin-url"),
        ("fetch", ws, "fork-sha", "origin"),
        ("detached", ws, "fork-sha"),
    ]
    assert result.branch == ""


def test_pr_execution_fetches_head_sha_from_fork_and_attaches_branch(
    tmp_path,
):
    calls = []
    repo = tmp_path / "repo"
    ws = tmp_path / "ws"

    class FakeGit:
        def ensure_base_clone(self, repository, url):
            calls.append(("cache", repository, url))
            return repo

        def clone_workspace(self, source, path, url):
            calls.append(("workspace", source, path, url))

        def fetch_commit(self, path, commit, remote):
            calls.append(("fetch", path, commit, remote))

        def checkout_branch(
            self, path, branch, base, start_point=None
        ):
            calls.append(
                ("checkout", path, branch, base, start_point)
            )

    result = GitWorkspaceManager(git_client=FakeGit()).prepare(
        WorkspaceRequest(
            "owner/repo#pr-4",
            "owner/repo",
            "topic",
            "main",
            workspace=str(ws),
            context=Context(
                {
                    "git": {
                        "repository_url": "https://github.com/fork/repo.git",
                        "base_repository_url": "https://github.com/owner/repo.git",
                        "fetch_url": "https://github.com/fork/repo.git",
                        "revision": "head-sha",
                    }
                }
            ),
        )
    )

    assert calls == [
        ("cache", "owner/repo", "https://github.com/owner/repo.git"),
        (
            "workspace",
            repo,
            ws,
            "https://github.com/owner/repo.git",
        ),
        (
            "fetch",
            ws,
            "head-sha",
            "https://github.com/fork/repo.git",
        ),
        ("checkout", ws, "topic", "main", "head-sha"),
    ]
    assert result.branch == "topic"


def test_review_prepare_propagates_unavailable_commit(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    ws = tmp_path / "ws"
    monkeypatch.setattr(git, "ensure_base_clone", lambda repository, url: repo)
    monkeypatch.setattr(git, "clone_workspace", lambda *args: None)
    monkeypatch.setattr(
        git,
        "fetch_commit",
        lambda *args: (_ for _ in ()).throw(git.GitError("unknown commit")),
    )

    with pytest.raises(git.GitError, match="unknown commit"):
        GitWorkspaceManager().prepare(
            WorkspaceRequest(
                "review:company/backend#4",
                "company/backend",
                "",
                "main",
                purpose="review",
                revision="missing",
                workspace=str(ws),
                context=Context({"git": {"repository_url": "origin-url"}}),
            )
        )
