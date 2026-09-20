"""Tests for Git operations against a real local bare repository (no network)."""

from __future__ import annotations

import os
import shutil
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

    assert git._environment_path(
        "ORCHESTRATOR_REPOS_DIR", "/unused"
    ) == Path.home() / "agent-repos"


def test_identity_bound_git_client_uses_selected_identity_environment(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Local User")
    monkeypatch.setattr(
        github_auth,
        "installation_token",
        lambda: (_ for _ in ()).throw(AssertionError()),
    )

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
            return subprocess.CompletedProcess(
                args, 0, "https://github.com/company/backend.git\n", ""
            )
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


def test_fetch_commit_uses_selected_identity_for_direct_github_url(
    monkeypatch, tmp_path
):
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
def repo_dir(remote_repo):
    return git.ensure_base_clone("test/gitrepo", f"file://{remote_repo}")


def _workspace(repo_dir, remote_repo, path, branch):
    git.clone_workspace(repo_dir, path, f"file://{remote_repo}")
    git.checkout_branch(path, branch, "main")
    return path


def test_ensure_base_clone_and_fetch(repo_dir):
    assert (repo_dir / ".git").is_dir()
    proc = subprocess.run(
        ["git", "branch", "-r"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    assert "origin/main" in proc.stdout


def test_detect_default_branch(repo_dir):
    assert git.detect_default_branch(repo_dir) == "main"


def test_workspace_clone_is_self_contained_and_pushes(
    repo_dir, remote_repo, tmp_path
):
    ws = _workspace(repo_dir, remote_repo, tmp_path / "ws", "ai/issue-1")

    assert (ws / ".git").is_dir()
    assert not (ws / ".git/objects/info/alternates").exists()
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert origin == f"file://{remote_repo}"

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


def test_independent_clones_can_checkout_same_branch_concurrently(
    repo_dir, remote_repo, tmp_path
):
    ws1 = _workspace(repo_dir, remote_repo, tmp_path / "ws1", "ai/shared")
    ws2 = _workspace(repo_dir, remote_repo, tmp_path / "ws2", "ai/shared")

    assert (ws1 / ".git").is_dir()
    assert (ws2 / ".git").is_dir()
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ws1,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "ai/shared"
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ws2,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "ai/shared"


def test_detached_checkout(repo_dir, remote_repo, tmp_path):
    ws = tmp_path / "review"
    git.clone_workspace(repo_dir, ws, f"file://{remote_repo}")
    revision = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    git.checkout_detached(ws, revision)

    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == ""


def test_commit_all_nothing_to_commit(repo_dir, remote_repo, tmp_path):
    ws = _workspace(repo_dir, remote_repo, tmp_path / "ws", "ai/issue-2")
    with pytest.raises(git.NoChangesError):
        git.commit_all(ws, "noop")


def test_workspace_exists_raises(repo_dir, remote_repo, tmp_path):
    ws = _workspace(repo_dir, remote_repo, tmp_path / "ws", "ai/issue-3")
    with pytest.raises(git.GitError, match="workspace already exists"):
        git.clone_workspace(repo_dir, ws, f"file://{remote_repo}")


def test_has_changes_ignores_agents_dir(repo_dir, remote_repo, tmp_path):
    ws = _workspace(repo_dir, remote_repo, tmp_path / "ws", "ai/issue-5")
    (ws / ".agents/plans").mkdir(parents=True)
    (ws / ".agents/plans/plan.md").write_text("plan")
    assert not git.has_changes(ws)


def test_commit_all_with_repo_gitignore_ignoring_agents(
    repo_dir, remote_repo, tmp_path
):
    ws = _workspace(repo_dir, remote_repo, tmp_path / "ws", "ai/issue-7")
    (ws / ".gitignore").write_text(".agents/\n")
    (ws / "work.txt").write_text("hello\nchange\n")
    (ws / ".agents/plans").mkdir(parents=True)
    (ws / ".agents/plans/plan.md").write_text("plan")
    git.commit_all(ws, "feat: with gitignore")
    proc = subprocess.run(
        ["git", "show", "--stat", "--format=", "HEAD"],
        cwd=ws,
        capture_output=True,
        text=True,
    )
    assert "work.txt" in proc.stdout
    assert ".agents" not in proc.stdout


def test_checkout_branch_reuses_existing_remote_branch(
    repo_dir, remote_repo, tmp_path
):
    ws1 = _workspace(repo_dir, remote_repo, tmp_path / "ws1", "ai/issue-11")
    (ws1 / "work.txt").write_text("hello\nv1\n")
    git.commit_all(ws1, "feat: v1")
    git.push_branch(ws1, "ai/issue-11")
    shutil.rmtree(ws1)

    ws2 = _workspace(repo_dir, remote_repo, tmp_path / "ws2", "ai/issue-11")
    assert (ws2 / "work.txt").read_text() == "hello\nv1\n"


def test_push_branch_force_overwrites_stale_remote(
    repo_dir, remote_repo, tmp_path
):
    ws1 = _workspace(repo_dir, remote_repo, tmp_path / "ws1", "ai/issue-10")
    (ws1 / "work.txt").write_text("hello\nv1\n")
    git.commit_all(ws1, "feat: v1")
    git.push_branch(ws1, "ai/issue-10")
    shutil.rmtree(ws1)

    ws2 = _workspace(repo_dir, remote_repo, tmp_path / "ws2", "ai/issue-10")
    (ws2 / "work.txt").write_text("hello\nv2\n")
    subprocess.run(
        ["git", "commit", "-a", "--amend", "-m", "feat: v2"],
        cwd=ws2,
        check=True,
    )
    proc = subprocess.run(
        ["git", "push", "origin", "ai/issue-10"],
        cwd=ws2,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0

    git.push_branch(ws2, "ai/issue-10")

    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ws2,
        capture_output=True,
        text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/ai/issue-10"],
        cwd=ws2,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    assert remote_head == local_head


def test_push_branch_does_not_force_arbitrary_pr_branch(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, cwd, *, check=True, env=None):
        calls.append(args)
        if args[:3] == ["git", "push", "-u"]:
            return subprocess.CompletedProcess(
                args, 1, "", "non-fast-forward"
            )
        raise AssertionError("force retry must not be attempted")

    monkeypatch.setattr(git, "_run", fake_run)
    with pytest.raises(git.GitError, match="non-fast-forward"):
        git.push_branch(
            tmp_path,
            "contributors/topic",
            "https://github.com/fork/repo.git",
            allow_force_with_lease=False,
        )
    assert calls == [
        [
            "git",
            "push",
            "-u",
            "https://github.com/fork/repo.git",
            "contributors/topic",
        ]
    ]
