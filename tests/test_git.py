"""Tests for git.py against a real local bare repository (no network)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.infra.git import client as git
from orchestrator.infra.github import auth as github_auth


def test_git_identity_uses_isolated_global_config():
    global_config = Path(os.environ["GIT_CONFIG_GLOBAL"])

    assert global_config.exists()
    assert global_config != Path.home() / ".gitconfig"
    assert subprocess.run(
        ["git", "config", "--global", "user.email"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "test@test"
    assert subprocess.run(
        ["git", "config", "--global", "user.name"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "test"


def test_git_environment_path_expands_home_directory(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_REPOS_DIR", "~/agent-repos")

    assert git._environment_path("ORCHESTRATOR_REPOS_DIR", "/unused") == Path.home() / "agent-repos"


def test_identity_bound_git_client_uses_selected_identity_environment(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Local User")
    monkeypatch.setattr(github_auth, "installation_token", lambda: (_ for _ in ()).throw(AssertionError()))

    environment = git.GitClient(github_auth.GitHubIdentity("user"))._call(
        git._github_env_for_url, "https://github.com/company/backend.git"
    )

    assert environment["GIT_AUTHOR_NAME"] == "Local User"


def test_fetch_commit_uses_selected_identity_for_named_remote(monkeypatch, tmp_path):
    selected_environment = {"GIT_AUTHOR_NAME": "Local User"}
    calls = []

    def fake_run(args, cwd, *, check=True, env=None):
        calls.append((args, cwd, env))
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/company/backend.git\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(git, "_run", fake_run)
    monkeypatch.setattr(
        github_auth.GitHubIdentity,
        "git_environment",
        lambda self: selected_environment,
    )

    git.GitClient(github_auth.GitHubIdentity("user")).fetch_commit(
        tmp_path, "a" * 40, remote="origin"
    )

    assert calls == [
        (["git", "remote", "get-url", "origin"], tmp_path, None),
        (["git", "fetch", "origin", "a" * 40], tmp_path, selected_environment),
    ]


def test_fetch_commit_uses_selected_identity_for_direct_github_url(monkeypatch, tmp_path):
    selected_environment = {"GIT_AUTHOR_NAME": "Local User"}
    calls = []

    def fake_run(args, cwd, *, check=True, env=None):
        calls.append((args, cwd, env))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(git, "_run", fake_run)
    monkeypatch.setattr(
        github_auth.GitHubIdentity,
        "git_environment",
        lambda self: selected_environment,
    )
    remote = "https://github.com/contributor/backend.git"

    git.GitClient(github_auth.GitHubIdentity("user")).fetch_commit(
        tmp_path, "a" * 40, remote=remote
    )

    assert calls == [
        (["git", "fetch", remote, "a" * 40], tmp_path, selected_environment),
    ]


@pytest.fixture
def repo_dir(remote_repo, tmp_path):
    return git.ensure_base_clone("test/gitrepo", f"file://{remote_repo}")


def test_ensure_base_clone_and_fetch(repo_dir):
    assert (repo_dir / ".git").exists()
    proc = subprocess.run(["git", "branch", "-r"], cwd=repo_dir, capture_output=True, text=True)
    assert "origin/main" in proc.stdout


def test_detect_default_branch(repo_dir):
    assert git.detect_default_branch(repo_dir) == "main"


def test_create_worktree_commits_and_pushes(repo_dir, remote_repo, tmp_path):
    ws = tmp_path / "ws"
    git.create_worktree(repo_dir, ws, "ai/issue-1", "main")
    assert ws.exists()

    (ws / "work.txt").write_text("hello\nimplemented\n")
    (ws / ".agents/plans").mkdir(parents=True)
    (ws / ".agents/plans/plan.md").write_text("plan")

    assert git.has_changes(ws)
    git.commit_all(ws, "feat: test\n\nCloses #1")
    git.push_branch(ws, "ai/issue-1")

    proc = subprocess.run(
        ["git", "show", "--stat", "--format=", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
    )
    assert "work.txt" in proc.stdout
    assert ".agents" not in proc.stdout

    proc = subprocess.run(
        ["git", "branch", "-r"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert "origin/ai/issue-1" in proc.stdout


def test_commit_all_nothing_to_commit(repo_dir, tmp_path):
    ws = tmp_path / "ws"
    git.create_worktree(repo_dir, ws, "ai/issue-2", "main")
    with pytest.raises(git.NoChangesError):
        git.commit_all(ws, "noop")


def test_workspace_exists_raises(repo_dir, tmp_path):
    ws = tmp_path / "ws"
    git.create_worktree(repo_dir, ws, "ai/issue-3", "main")
    with pytest.raises(git.GitError):
        git.create_worktree(repo_dir, ws, "ai/issue-4", "main")


def test_has_changes_ignores_agents_dir(repo_dir, tmp_path):
    ws = tmp_path / "ws"
    git.create_worktree(repo_dir, ws, "ai/issue-5", "main")
    (ws / ".agents/plans").mkdir(parents=True)
    (ws / ".agents/plans/plan.md").write_text("plan")
    assert not git.has_changes(ws)


def test_commit_all_with_repo_gitignore_ignoring_agents(repo_dir, tmp_path):
    """Repo's own .gitignore ignores .agents: the exclude pathspec fails, and the
    fallback plain add must skip .agents anyway."""
    ws = tmp_path / "ws"
    git.create_worktree(repo_dir, ws, "ai/issue-7", "main")
    (ws / ".gitignore").write_text(".agents/\n")
    (ws / "work.txt").write_text("hello\nchange\n")
    (ws / ".agents/plans").mkdir(parents=True)
    (ws / ".agents/plans/plan.md").write_text("plan")
    git.commit_all(ws, "feat: with gitignore")
    proc = subprocess.run(["git", "show", "--stat", "--format=", "HEAD"], cwd=ws, capture_output=True, text=True)
    assert "work.txt" in proc.stdout
    assert ".agents" not in proc.stdout


def test_push_branch_force_overwrites_stale_remote(repo_dir, remote_repo, tmp_path):
    # First run: push ai/issue-10 to the remote.
    ws1 = tmp_path / "ws1"
    git.create_worktree(repo_dir, ws1, "ai/issue-10", "main")
    (ws1 / "work.txt").write_text("hello\nv1\n")
    git.commit_all(ws1, "feat: v1")
    git.push_branch(ws1, "ai/issue-10")

    # Clean up locally (as a re-run would), leaving the stale remote branch behind.
    git.remove_worktree(repo_dir, ws1, "ai/issue-10")

    # Re-run: fresh worktree on the same branch name, different history -> plain
    # push is a non-fast-forward and must be recovered by force-with-lease.
    ws2 = tmp_path / "ws2"
    git.create_worktree(repo_dir, ws2, "ai/issue-10", "main")
    (ws2 / "work.txt").write_text("hello\nv2\n")
    git.commit_all(ws2, "feat: v2")
    proc = subprocess.run(["git", "push", "origin", "ai/issue-10"], cwd=ws2, capture_output=True, text=True)
    assert proc.returncode != 0  # rejected: non-fast-forward

    git.push_branch(ws2, "ai/issue-10")  # must succeed via force-with-lease

    local_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws2, capture_output=True, text=True).stdout.strip()
    remote_head = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/ai/issue-10"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    assert remote_head == local_head
