"""End-to-end graph tests: real git + fake opencode + mocked gh PR creation."""

from __future__ import annotations

import subprocess

import pytest

from orchestrator import config, state as state_mod, workspace
from orchestrator.graph import build_graph, parse_verdict
from orchestrator.persistence import TaskStore


def _seed(remote_repo: str, issue_number: int = 1) -> dict:
    return {
        "task_id": f"company/backend#{issue_number}",
        "repository": "company/backend",
        "issue_number": issue_number,
        "issue_title": "Add a feature",
        "issue_body": "Please implement the feature.",
        "repository_url": f"file://{remote_repo}",
        "base_branch": "main",
        "branch": f"ai/issue-{issue_number}",
        "status": state_mod.RECEIVED,
        "iteration": 1,
    }


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "db.sqlite")


def test_full_flow(remote_repo, allowlist, store, monkeypatch):
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 42)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo), config={"configurable": {"thread_id": "company/backend#1"}})

    assert result["status"] == state_mod.COMPLETED
    assert result["pr_number"] == 42
    assert result["workspace"] == str(workspace.task_workspace("company/backend", 1))

    # Cleanup ran: worktree and local branch are gone.
    ws = workspace.task_workspace("company/backend", 1)
    assert not ws.exists()
    proc = subprocess.run(
        ["git", "branch", "--list", "ai/issue-1"],
        cwd=config.REPOS_DIR / "company-backend",
        capture_output=True,
        text=True,
    )
    assert "ai/issue-1" not in proc.stdout

    # The commit lives on the pushed branch: verify via the base clone.
    proc = subprocess.run(
        ["git", "show", "--stat", "--format=", "origin/ai/issue-1"],
        cwd=config.REPOS_DIR / "company-backend",
        capture_output=True,
        text=True,
    )
    assert "work.txt" in proc.stdout
    assert ".agents" not in proc.stdout

    proc = subprocess.run(["git", "branch", "-r"], cwd=config.REPOS_DIR / "company-backend", capture_output=True, text=True)
    assert "origin/ai/issue-1" in proc.stdout

    assert (config.LOGS_DIR / "company-backend-1" / "plan.log").exists()
    assert (config.LOGS_DIR / "company-backend-1" / "review.log").exists()


def test_disallowed_repository(remote_repo, store):
    seed = _seed(remote_repo)
    seed["repository"] = "evil/repo"
    graph = build_graph(store.checkpointer())
    result = graph.invoke(seed, config={"configurable": {"thread_id": "evil/repo#1"}})
    assert result["status"] == state_mod.FAILED
    assert "allowlist" in result["error"]


def test_opencode_failure_fails_task(remote_repo, allowlist, store, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_FAIL", "1")
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 2), config={"configurable": {"thread_id": "company/backend#2"}})
    assert result["status"] == state_mod.FAILED
    assert "exited with 1" in result["error"]
    # Cleanup only runs on success: the worktree is kept for debugging.
    assert workspace.task_workspace("company/backend", 2).exists()


def test_changes_required_still_creates_pr(remote_repo, allowlist, store, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_VERDICT", "CHANGES_REQUIRED")
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 43)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 3), config={"configurable": {"thread_id": "company/backend#3"}})
    assert result["status"] == state_mod.COMPLETED
    assert result["review_verdict"] == "CHANGES_REQUIRED"
    assert result["pr_number"] == 43


def test_create_pr_with_existing_commit(remote_repo, allowlist, store, monkeypatch):
    """Implement agent already committed: create_pr must push + open PR without a new commit."""
    from orchestrator import git as git_mod
    from orchestrator.graph import create_pr

    seed = _seed(remote_repo, 4)
    ws = workspace.task_workspace("company/backend", 4)
    repo_dir = git_mod.ensure_base_clone("company/backend", f"file://{remote_repo}")
    git_mod.create_worktree(repo_dir, ws, seed["branch"], "main")
    (ws / "work.txt").write_text("hello\ncommitted\n")
    git_mod.commit_all(ws, "feat: agent committed\n\nCloses #4")
    seed.update({"workspace": str(ws), "status": state_mod.REVIEWING})

    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 44)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert updates["pr_number"] == 44


def test_create_pr_reuses_existing_pr(remote_repo, allowlist, store, monkeypatch):
    """When an open PR already exists for the branch, create_pr must reuse it."""
    from orchestrator import git as git_mod
    from orchestrator.graph import create_pr

    seed = _seed(remote_repo, 5)
    ws = workspace.task_workspace("company/backend", 5)
    repo_dir = git_mod.ensure_base_clone("company/backend", f"file://{remote_repo}")
    git_mod.create_worktree(repo_dir, ws, seed["branch"], "main")
    (ws / "work.txt").write_text("hello\ncommitted\n")
    git_mod.commit_all(ws, "feat: agent committed\n\nCloses #5")
    seed.update({"workspace": str(ws), "status": state_mod.REVIEWING})

    created: list = []
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: 77)
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: created.append(1) or 0)

    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert updates["pr_number"] == 77
    assert created == []  # no new PR created


def test_parse_verdict():
    assert parse_verdict("all good\nVERDICT: APPROVED\n") == "APPROVED"
    assert parse_verdict("VERDICT: CHANGES_REQUIRED") == "CHANGES_REQUIRED"
    assert parse_verdict("no verdict here") == "NEEDS_CLARIFICATION"