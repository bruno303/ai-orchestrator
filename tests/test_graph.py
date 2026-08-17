"""End-to-end graph tests: real git + fake opencode + mocked gh PR creation."""

from __future__ import annotations

import subprocess

import pytest

from orchestrator import config, state as state_mod, workspace
from orchestrator.graph import build_graph, parse_verdict
from orchestrator.persistence import TaskStore


class _FakePR:
    def __init__(self, body: str):
        self.body = body


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
    assert result["phase_attempts"] == 1
    # Cleanup only runs on success: the worktree is kept for debugging.
    assert workspace.task_workspace("company/backend", 2).exists()


def test_opencode_error_fails_task_with_current_attempt(remote_repo, allowlist, store, monkeypatch):
    from orchestrator import opencode

    def raise_opencode_error(*args, **kwargs):
        raise opencode.OpenCodeError("opencode unavailable")

    monkeypatch.setattr("orchestrator.opencode.run_opencode", raise_opencode_error)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 12), config={"configurable": {"thread_id": "company/backend#12"}})

    assert result["status"] == state_mod.FAILED
    assert result["error"] == "opencode unavailable"
    assert result["phase_attempts"] == 1


def test_plan_persists_planner_output_when_artifact_is_missing(tmp_path, monkeypatch):
    from orchestrator import opencode
    from orchestrator.graph import plan

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    monkeypatch.setattr(
        "orchestrator.opencode.run_opencode",
        lambda **kwargs: opencode.OpenCodeResult(0, "# Implementation Plan\n\nDo the work.", "", 1.0),
    )
    result = plan(
        {
            "task_id": "company/backend#13",
            "workspace": str(workspace_path),
            "issue_number": 13,
            "repository": "company/backend",
            "issue_title": "Add a feature",
            "issue_body": "Please implement the feature.",
        }
    )

    assert result["status"] == state_mod.PLANNING
    assert result["plan_path"] == ".agents/plans/plan.md"
    assert (workspace_path / ".agents/plans/plan.md").read_text() == "# Implementation Plan\n\nDo the work.\n"


def test_plan_fails_when_planner_output_is_empty(tmp_path, monkeypatch):
    from orchestrator import opencode
    from orchestrator.graph import plan

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    monkeypatch.setattr(
        "orchestrator.opencode.run_opencode",
        lambda **kwargs: opencode.OpenCodeResult(0, "", "", 1.0),
    )
    result = plan(
        {
            "task_id": "company/backend#14",
            "workspace": str(workspace_path),
            "issue_number": 14,
            "repository": "company/backend",
            "issue_title": "Add a feature",
            "issue_body": "Please implement the feature.",
        }
    )

    assert result["status"] == state_mod.FAILED
    assert result["error"] == "planning completed without creating .agents/plans/plan.md"


def test_changes_required_still_creates_pr(remote_repo, allowlist, store, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_VERDICT", "CHANGES_REQUIRED")
    captured: dict = {}
    monkeypatch.setattr(
        "orchestrator.github.create_pull_request",
        lambda *a, **k: captured.update(body=a[2]) or 43,
    )
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 3), config={"configurable": {"thread_id": "company/backend#3"}})
    assert result["status"] == state_mod.COMPLETED
    assert result["review_verdict"] == "CHANGES_REQUIRED"
    assert result["pr_number"] == 43
    body = captured["body"]
    assert body.startswith("Closes #3")
    assert "## Last Review report" in body
    assert "- status: CHANGES_REQUIRED" in body
    assert "- findings:" in body
    assert "Missing structured findings in the review output." in body
    assert "VERDICT: CHANGES_REQUIRED" not in body


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
    monkeypatch.setattr(
        "orchestrator.github.get_pull_request",
        lambda *a, **k: _FakePR("Closes #5"),
    )

    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert updates["pr_number"] == 77
    assert created == []  # no new PR created


def test_parse_verdict():
    assert parse_verdict("all good\nVERDICT: APPROVED\n") == "APPROVED"
    assert parse_verdict("VERDICT: CHANGES_REQUIRED") == "CHANGES_REQUIRED"
    assert parse_verdict("no verdict here") == "NEEDS_CLARIFICATION"


def test_create_pr_reuse_prepends_review_section(remote_repo, allowlist, store, monkeypatch):
    """On reuse, non-approved review text is prepended after `Closes #n`, keeping history."""
    from orchestrator import git as git_mod
    from orchestrator.graph import create_pr

    seed = _seed(remote_repo, 6)
    ws = workspace.task_workspace("company/backend", 6)
    repo_dir = git_mod.ensure_base_clone("company/backend", f"file://{remote_repo}")
    git_mod.create_worktree(repo_dir, ws, seed["branch"], "main")
    (ws / "work.txt").write_text("hello\ncommitted\n")
    git_mod.commit_all(ws, "feat: agent committed\n\nCloses #6")
    seed.update(
        {
            "workspace": str(ws),
            "status": state_mod.REVIEWING,
            "review_verdict": "CHANGES_REQUIRED",
            "review_result": "REVIEW_STATUS: CHANGES_REQUIRED\nFINDINGS:\n- New issues found.",
        }
    )

    old_body = "Closes #6\n\n## Last Review report\n- status: CHANGES_REQUIRED\n- findings:\n  - Old issues."
    edited: list = []
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: 77)
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 0)
    monkeypatch.setattr("orchestrator.github.get_pull_request", lambda *a, **k: _FakePR(old_body))
    monkeypatch.setattr(
        "orchestrator.github.update_pull_request_body",
        lambda *a, **k: edited.append((a[1], a[2])),
    )

    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    pr_number, new_body = edited[0]
    assert pr_number == 77
    assert new_body.startswith("Closes #6")
    assert new_body.count("Closes #6") == 1
    assert "## Last Review report" in new_body
    assert "- findings:" in new_body
    assert new_body.index("New issues found.") < new_body.index("Old issues.")


def test_create_pr_reuse_approved_does_not_edit(remote_repo, allowlist, store, monkeypatch):
    """On reuse with an approved (or absent) review, the PR body is left untouched."""
    from orchestrator import git as git_mod
    from orchestrator.graph import create_pr

    seed = _seed(remote_repo, 7)
    ws = workspace.task_workspace("company/backend", 7)
    repo_dir = git_mod.ensure_base_clone("company/backend", f"file://{remote_repo}")
    git_mod.create_worktree(repo_dir, ws, seed["branch"], "main")
    (ws / "work.txt").write_text("hello\ncommitted\n")
    git_mod.commit_all(ws, "feat: agent committed\n\nCloses #7")
    seed.update({"workspace": str(ws), "status": state_mod.REVIEWING})

    edited: list = []
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: 77)
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 0)
    monkeypatch.setattr("orchestrator.github.get_pull_request", lambda *a, **k: _FakePR("Closes #7"))
    monkeypatch.setattr(
        "orchestrator.github.update_pull_request_body",
        lambda *a, **k: edited.append(1),
    )

    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert edited == []  # approved -> no PR body edit


def test_implement_retries_once_then_succeeds(remote_repo, allowlist, store, monkeypatch, tmp_path):
    """The implement phase loops on the primary model; the fallback succeeds. Both must be attempted.

    FAKE_OPCODE_LOOP_PROMPT scopes the loop to the implement phase only, so the
    plan/test/review phases run normally and the retry is genuinely exercised by
    implement (not consumed by an earlier phase).
    """
    # MODEL_PRIMARY/MODEL_FALLBACK are bound at import time, so the cache clear in
    # clear_config_cache cannot change them per-test; patch the module attributes
    # directly since graph.py reads config.MODEL_PRIMARY at call time.
    monkeypatch.setattr(config, "MODEL_PRIMARY", config.ModelConfig("verboo/deepseek-v4-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK", config.ModelConfig("verboo/glm-4.7-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK_ENABLED", True)
    monkeypatch.setenv("FAKE_OPCODE_LOOP_ONCE", "verboo/deepseek-v4-flash")
    monkeypatch.setenv("FAKE_OPCODE_LOOP_PROMPT", "implementing GitHub issue")
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_OPCODE_MODEL_FILE", str(model_file))
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 42)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 10), config={"configurable": {"thread_id": "company/backend#10"}})
    assert result["status"] == state_mod.COMPLETED
    # The fake writes a line for every opencode invocation (plan, implement x2, test, review),
    # so both the primary and the fallback model must appear in the file.
    lines = model_file.read_text().splitlines()
    assert any("model=verboo/deepseek-v4-flash" in line for line in lines)
    assert any("model=verboo/glm-4.7-flash" in line for line in lines)
    # The implement phase looped once, but the later first-attempt phases overwrite it.
    assert result["phase_attempts"] == 1


def test_implement_fails_after_max_attempts(remote_repo, allowlist, store, monkeypatch):
    """Degenerate output in implement on every attempt must fail the task after PHASE_MAX_ATTEMPTS.

    FAKE_OPCODE_LOOP_PROMPT scopes the loop to implement, so plan/test/review run
    normally and the failure comes from the implement phase itself.
    """
    # See test_implement_retries_once_then_succeeds: import-time binding of the
    # MODEL_PRIMARY/MODEL_FALLBACK constants requires direct module attribute patching.
    monkeypatch.setattr(config, "MODEL_PRIMARY", config.ModelConfig("verboo/deepseek-v4-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK", config.ModelConfig("verboo/glm-4.7-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK_ENABLED", True)
    monkeypatch.setenv("FAKE_OPCODE_LOOP", "1")
    monkeypatch.setenv("FAKE_OPCODE_LOOP_PROMPT", "implementing GitHub issue")
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 42)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 11), config={"configurable": {"thread_id": "company/backend#11"}})
    assert result["status"] == state_mod.FAILED
    assert "degenerate output" in result["error"]
    assert result["phase_attempts"] == config.PHASE_MAX_ATTEMPTS
