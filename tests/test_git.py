"""Tests for git.py against a real local bare repository (no network)."""

from __future__ import annotations

import subprocess

import pytest

from orchestrator import git


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