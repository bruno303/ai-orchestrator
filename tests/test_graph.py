"""End-to-end graph tests: real git + fake opencode + mocked gh PR creation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import config, state as state_mod, workspace
from orchestrator.graph import _run_opencode, build_graph, cleanup, create_pr, prepare_workspace
from orchestrator.persistence import TaskStore
from orchestrator.providers import ExecutionResult, PublicationResult, WorkspaceResult


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
    nodes: list[str] = []
    graph = build_graph(store.checkpointer(), on_node_start=lambda node, state: nodes.append(node))
    result = graph.invoke(_seed(remote_repo), config={"configurable": {"thread_id": "company/backend#1"}})

    assert result["status"] == state_mod.COMPLETED
    assert result["output"]["external_id"] == "42"
    assert result["workspace"]["path"] == str(workspace.task_workspace("company/backend", 1))
    assert result["processing"]["context"]["github"]["issue_number"] == 1
    assert "review_verdict" not in result["processing"]
    assert "review_result" not in result["processing"]
    assert nodes == ["prepare_workspace", "plan", "implement", "test", "create_pr", "cleanup"]

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
    assert not (config.LOGS_DIR / "company-backend-1" / "review.log").exists()


def test_successful_test_is_followed_immediately_by_create_pr(remote_repo, allowlist, store, monkeypatch):
    nodes: list[str] = []
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 46)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)

    result = build_graph(
        store.checkpointer(), on_node_start=lambda node, state: nodes.append(node)
    ).invoke(_seed(remote_repo, 17), config={"configurable": {"thread_id": "company/backend#17"}})

    assert result["status"] == state_mod.COMPLETED
    assert nodes[nodes.index("test") + 1] == "create_pr"


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


def test_executor_reported_failure_fails_even_with_zero_exit_code(remote_repo, allowlist, store):
    class FailingExecutor:
        def execute(self, request):
            return ExecutionResult(False, 0, provider_state={"executor_run": "rejected"})

    graph = build_graph(store.checkpointer(), executor=FailingExecutor())
    result = graph.invoke(_seed(remote_repo, 13), config={"configurable": {"thread_id": "company/backend#13"}})

    assert result["status"] == state_mod.FAILED
    assert "reported failure" in result["error"]
    assert result["processing"]["context"]["opencode"] == {"executor_run": "rejected"}


def test_executor_provider_state_is_forwarded_to_next_phase(tmp_path):
    calls: list[dict] = []

    class StatefulExecutor:
        def execute(self, request):
            calls.append(request.provider_state)
            return ExecutionResult(True, 0, provider_state={"session_id": "session-1"})

    state = {
        "task_id": "company/backend#14",
        "workspace": {"path": str(tmp_path)},
        "processing": {},
    }
    first_update, _ = _run_opencode(state, "plan", "plan", "plan", StatefulExecutor())
    _run_opencode({**state, **first_update}, "implement", "build", "implement", StatefulExecutor())

    assert calls[1]["session_id"] == "session-1"


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


def test_graph_uses_injected_executor(remote_repo, allowlist, store, monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeExecutor:
        def execute(self, request):
            calls.append((request.agent, request.prompt))
            if request.agent == "plan":
                return ExecutionResult(True, 0, stdout="# Plan\n\nDo it.", provider_state={"executor_run": "plan"})
            if request.agent == "build":
                if "Implement work item" in request.prompt:
                    Path(request.workspace, "work.txt").write_text("implemented\n")
                    return ExecutionResult(True, 0, stdout="implemented", provider_state={"executor_run": "implement"})
                return ExecutionResult(True, 0, stdout="tested", provider_state={"executor_run": "test"})
            raise AssertionError(f"unexpected executor request: {request.agent}")

    monkeypatch.setattr("orchestrator.opencode.run_opencode", lambda **kwargs: pytest.fail("direct OpenCode call"))
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 45)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer(), executor=FakeExecutor())
    result = graph.invoke(_seed(remote_repo, 15), config={"configurable": {"thread_id": "company/backend#15"}})

    assert result["status"] == state_mod.COMPLETED
    assert [agent for agent, _ in calls] == ["plan", "build", "build"]
    assert result["processing"]["context"]["opencode"] == {"executor_run": "test"}


def test_executor_provider_state_survives_failure(remote_repo, allowlist, store, monkeypatch):
    class FailingExecutor:
        def execute(self, request):
            return ExecutionResult(False, 1, provider_state={"executor_run": "failed"})

    graph = build_graph(store.checkpointer(), executor=FailingExecutor())
    result = graph.invoke(_seed(remote_repo, 16), config={"configurable": {"thread_id": "company/backend#16"}})

    assert result["status"] == state_mod.FAILED
    assert result["processing"]["context"]["opencode"] == {"executor_run": "failed"}
    assert "provider_state" not in result


def test_graph_uses_injected_workspace_and_destination(tmp_path, allowlist, store):
    workspace_path = tmp_path / "workspace"
    calls: list[str] = []

    class FakeWorkspaceManager:
        def prepare(self, request):
            calls.append("prepare")
            workspace_path.mkdir()
            return WorkspaceResult(
                str(workspace_path), request.branch,
                {"base_branch": "main", "provider_token": "keep-me"},
            )

        def cleanup(self, result):
            calls.append("cleanup")
            assert result.provider_state == {"base_branch": "main", "provider_token": "keep-me"}

    class FakeDestination:
        def publish(self, request):
            calls.append("publish")
            return PublicationResult(number=99)

    class FakeExecutor:
        def execute(self, request):
            if request.agent == "plan":
                return ExecutionResult(True, 0, stdout="# Plan\n\nDo it.")
            return ExecutionResult(True, 0, stdout="phase complete")

    result = build_graph(
        store.checkpointer(), executor=FakeExecutor(), workspace_manager=FakeWorkspaceManager(),
        destination=FakeDestination(),
    ).invoke(_seed("unused", 20), config={"configurable": {"thread_id": "company/backend#20"}})
    assert result["status"] == state_mod.COMPLETED
    assert result["output"]["external_id"] == "99"
    assert calls == ["prepare", "publish", "cleanup"]


def test_prepare_workspace_uses_manager_default_when_base_branch_is_missing(allowlist, tmp_path):
    class FakeWorkspaceManager:
        def prepare(self, request):
            assert request.base_branch == ""
            return WorkspaceResult(
                str(tmp_path / "workspace"), request.branch,
                {"base_branch": "develop", "provider_token": "resolved"},
            )

        def cleanup(self, result):
            raise AssertionError("cleanup should not be called")

    seed = _seed("unused", 21)
    seed.pop("base_branch")
    result = prepare_workspace(seed, FakeWorkspaceManager())

    assert result["workspace"]["base_branch"] == "develop"
    assert result["workspace"]["context"]["workspace"] == {"base_branch": "develop", "provider_token": "resolved"}


def test_prepare_workspace_preserves_requested_base_branch(allowlist, tmp_path):
    class FakeWorkspaceManager:
        def prepare(self, request):
            assert request.base_branch == "main"
            return WorkspaceResult(str(tmp_path / "workspace"), request.branch, {})

        def cleanup(self, result):
            raise AssertionError("cleanup should not be called")

    result = prepare_workspace(_seed("unused", 22), FakeWorkspaceManager())

    assert result["workspace"]["base_branch"] == "main"


def test_prepare_workspace_forwards_provider_state(allowlist, tmp_path):
    class StatefulWorkspaceManager:
        def prepare(self, request):
            assert request.provider_state["remote_id"] == "workspace-1"
            return WorkspaceResult(str(tmp_path / "workspace"), request.branch, request.provider_state)

        def cleanup(self, result):
            raise AssertionError("cleanup should not be called")

    seed = _seed("unused", 23)
    seed["workspace"] = {
        "branch": "ai/issue-23",
        "base_branch": "main",
        "provider_state": {"remote_id": "workspace-1"},
    }

    prepare_workspace(seed, StatefulWorkspaceManager())


def test_generic_runtime_does_not_own_github_pr_body_logic():
    import orchestrator.graph as graph
    assert not hasattr(graph, "_pr_body")


def test_new_graph_updates_are_namespace_only(allowlist, tmp_path):
    class FakeWorkspaceManager:
        provider_type = "custom_workspace"

        def prepare(self, request):
            return WorkspaceResult(str(tmp_path / "workspace"), request.branch, {"base_branch": "main"})

        def cleanup(self, result):
            pass

    seed = {
        "task_id": "company/backend#30",
        "input": {"provider": "custom_input", "data": {"repository": "company/backend", "number": 30}},
        "processing": {},
        "workspace": {"path": str(tmp_path / "workspace"), "branch": "task-30", "base_branch": "main"},
        "output": {},
        "status": state_mod.RECEIVED,
    }
    update = prepare_workspace(seed, FakeWorkspaceManager())

    assert set(update) <= {"input", "workspace", "status"}
    assert not {"repository", "issue_number", "branch", "workspace_path", "provider_state"} & set(update)
    assert update["workspace"]["context"]["workspace"]["base_branch"] == "main"


def test_namespace_only_checkpoint_retains_phase_results(allowlist, store, tmp_path):
    calls: list[str] = []

    class FakeWorkspaceManager:
        provider_type = "custom_workspace"

        def prepare(self, request):
            path = tmp_path / "workspace"
            path.mkdir()
            return WorkspaceResult(str(path), request.branch, {"base_branch": "main"})

        def cleanup(self, result):
            pass

    class FakeExecutor:
        def execute(self, request):
            calls.append(request.agent)
            if request.agent == "plan":
                return ExecutionResult(True, 0, stdout="# Plan\n\nDo it.")
            return ExecutionResult(True, 0, stdout="phase complete")

    class FakeDestination:
        provider_type = "custom_destination"

        def publish(self, request):
            return PublicationResult(number=123, provider_state={"remote_id": "123"})

    seed = {
        "task_id": "company/backend#31",
        "input": {"provider": "custom_input", "data": {"repository": "company/backend", "number": 31,
            "title": "Title", "body": "Body"}, "provider_state": {}},
        "processing": {
            "review_verdict": "APPROVED",
            "review_result": "legacy metadata",
        },
        "workspace": {"path": str(tmp_path / "workspace"), "branch": "task-31", "base_branch": "main"},
        "output": {}, "status": state_mod.RECEIVED,
    }
    graph = build_graph(
        store.checkpointer(), executor=FakeExecutor(), workspace_manager=FakeWorkspaceManager(),
        destination=FakeDestination(),
    )
    result = graph.invoke(seed, config={"configurable": {"thread_id": seed["task_id"]}})

    assert calls == ["plan", "build", "build"]
    assert result["processing"]["review_verdict"] == "APPROVED"
    assert result["output"]["context"]["destination"]["remote_id"] == "123"
    assert not {"repository", "issue_number", "pr_number"} & set(result)


def test_injected_provider_identity_is_written_to_namespaces(allowlist, tmp_path):
    class NamedWorkspace:
        provider_type = "remote_workspace"

        def prepare(self, request):
            return WorkspaceResult(str(tmp_path / "workspace"), request.branch, {"base_branch": "main"})

        def cleanup(self, result):
            pass

    result = prepare_workspace(_seed("unused", 30), NamedWorkspace())
    assert result["workspace"]["provider"] == "remote_workspace"

    class NamedDestination:
        provider_type = "remote_destination"

        def publish(self, request):
            return PublicationResult(number=301, provider_state={"remote_id": "301"})

    state = {**_seed("unused", 31), "workspace": {"path": str(tmp_path), "branch": "branch", "base_branch": "main"},
             "processing": {"review_verdict": "APPROVED"}}
    result = create_pr(state, NamedDestination())
    assert result["output"]["provider"] == "remote_destination"
    assert result["output"]["context"]["destination"]["remote_id"] == "301"


def test_destination_publication_url_is_retained(allowlist, tmp_path):
    class DestinationWithUrl:
        provider_type = "artifact_store"

        def publish(self, request):
            return PublicationResult(url="https://artifacts.example/run/1")

    state = {
        **_seed("unused", 34),
        "input": {"data": {"repository": "company/backend", "number": 34, "title": "Title", "body": "Body"}},
        "workspace": {"path": str(tmp_path), "branch": "branch", "base_branch": "main"},
        "processing": {},
    }

    result = create_pr(state, DestinationWithUrl())

    assert result["output"]["url"] == "https://artifacts.example/run/1"


def test_cleanup_recovers_repository_for_old_git_checkpoint(monkeypatch, tmp_path):
    from orchestrator import git

    captured = {}
    monkeypatch.setattr(git, "base_repo_dir", lambda repository: tmp_path / repository.replace("/", "-"))
    monkeypatch.setattr(git, "remove_worktree", lambda *args: captured.update(args=args))
    result = cleanup({
        "task_id": "company/backend#32",
        "input": {"data": {"repository": "company/backend", "number": 32}},
        "workspace": {"path": str(tmp_path / "worktree"), "branch": "ai/issue-32", "provider_state": {}},
    })
    assert result["status"] == state_mod.COMPLETED
    assert captured["args"][0] == tmp_path / "company-backend"


def test_workspace_provider_state_is_validated_at_graph_boundary(allowlist):
    class InvalidWorkspace:
        def prepare(self, request):
            return WorkspaceResult("/tmp/workspace", request.branch, {"invalid": object()})

        def cleanup(self, result):
            pass

    result = prepare_workspace(_seed("unused", 33), InvalidWorkspace())
    assert result["status"] == state_mod.FAILED
    assert "JSON-serializable" in result["error"]


def test_injected_workspace_prepare_failure_does_not_cleanup(allowlist, store):
    calls: list[str] = []

    class FailingWorkspaceManager:
        def prepare(self, request):
            calls.append("prepare")
            raise RuntimeError("provider unavailable")

        def cleanup(self, result):
            calls.append("cleanup")

    result = build_graph(
        store.checkpointer(), workspace_manager=FailingWorkspaceManager(),
    ).invoke(_seed("unused", 22), config={"configurable": {"thread_id": "company/backend#22"}})

    assert result["status"] == state_mod.FAILED
    assert result["error"] == "provider unavailable"
    assert calls == ["prepare"]


def test_injected_workspace_cleanup_failure_does_not_fail_task(tmp_path, allowlist, store):
    workspace_path = tmp_path / "workspace"

    class WorkspaceManager:
        def prepare(self, request):
            workspace_path.mkdir()
            return WorkspaceResult(
                str(workspace_path), request.branch,
                {"base_branch": "main", "provider_token": "lifecycle"},
            )

        def cleanup(self, result):
            assert result.provider_state["provider_token"] == "lifecycle"
            raise RuntimeError("cleanup unavailable")

    class FakeExecutor:
        def execute(self, request):
            if request.agent == "plan":
                return ExecutionResult(True, 0, stdout="# Plan\n\nDo it.")
            return ExecutionResult(True, 0, stdout="phase complete")

    class FakeDestination:
        def publish(self, request):
            return PublicationResult(number=100)

    result = build_graph(
        store.checkpointer(), executor=FakeExecutor(), workspace_manager=WorkspaceManager(),
        destination=FakeDestination(),
    ).invoke(_seed("unused", 23), config={"configurable": {"thread_id": "company/backend#23"}})

    assert result["status"] == state_mod.COMPLETED


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
    assert result["processing"]["plan_path"] == ".agents/plans/plan.md"
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
    seed.update({"workspace": str(ws), "status": state_mod.CREATING_PR})

    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 44)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert updates["output"]["external_id"] == "44"


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
    seed.update({"workspace": str(ws), "status": state_mod.CREATING_PR})

    created: list = []
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: 77)
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: created.append(1) or 0)
    monkeypatch.setattr(
        "orchestrator.github.get_pull_request",
        lambda *a, **k: _FakePR("Closes #5"),
    )

    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert updates["output"]["external_id"] == "77"
    assert created == []  # no new PR created


def test_create_pr_reuse_normalized_does_not_edit(remote_repo, allowlist, store, monkeypatch):
    """On reuse without review metadata, the existing PR body is left untouched."""
    from orchestrator import git as git_mod
    from orchestrator.graph import create_pr

    seed = _seed(remote_repo, 7)
    ws = workspace.task_workspace("company/backend", 7)
    repo_dir = git_mod.ensure_base_clone("company/backend", f"file://{remote_repo}")
    git_mod.create_worktree(repo_dir, ws, seed["branch"], "main")
    (ws / "work.txt").write_text("hello\ncommitted\n")
    git_mod.commit_all(ws, "feat: agent committed\n\nCloses #7")
    seed.update({"workspace": str(ws), "status": state_mod.CREATING_PR})

    edited: list = []
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: 77)
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 0)
    existing_body = "Closes #7\n\nExisting PR description"
    monkeypatch.setattr("orchestrator.github.get_pull_request", lambda *a, **k: _FakePR(existing_body))
    monkeypatch.setattr(
        "orchestrator.github.update_pull_request_body",
        lambda *a, **k: edited.append(1),
    )

    updates = create_pr(seed)
    assert updates["status"] == state_mod.COMPLETED
    assert edited == []


def test_implement_retries_once_then_succeeds(remote_repo, allowlist, store, monkeypatch, tmp_path):
    """The implement phase loops on the primary model; the fallback succeeds. Both must be attempted.

    FAKE_OPCODE_LOOP_PROMPT scopes the loop to the implement phase only, so the
    plan/test phases run normally and the retry is genuinely exercised by
    implement (not consumed by an earlier phase).
    """
    # MODEL_PRIMARY/MODEL_FALLBACK are bound at import time, so the cache clear in
    # clear_config_cache cannot change them per-test; patch the module attributes
    # directly since graph.py reads config.MODEL_PRIMARY at call time.
    monkeypatch.setattr(config, "MODEL_PRIMARY", config.ModelConfig("verboo/deepseek-v4-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK", config.ModelConfig("verboo/glm-4.7-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK_ENABLED", True)
    monkeypatch.setenv("FAKE_OPCODE_LOOP_ONCE", "verboo/deepseek-v4-flash")
    monkeypatch.setenv("FAKE_OPCODE_LOOP_PROMPT", "Implement work item")
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_OPCODE_MODEL_FILE", str(model_file))
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 42)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 10), config={"configurable": {"thread_id": "company/backend#10"}})
    assert result["status"] == state_mod.COMPLETED
    # The fake writes a line for every opencode invocation (plan, implement x2, test),
    # so both the primary and the fallback model must appear in the file.
    lines = model_file.read_text().splitlines()
    assert any("model=verboo/deepseek-v4-flash" in line for line in lines)
    assert any("model=verboo/glm-4.7-flash" in line for line in lines)
    # The implement phase looped once, but the later first-attempt phases overwrite it.
    assert result["phase_attempts"] == 1


def test_implement_fails_after_max_attempts(remote_repo, allowlist, store, monkeypatch):
    """Degenerate output in implement on every attempt must fail the task after PHASE_MAX_ATTEMPTS.

    FAKE_OPCODE_LOOP_PROMPT scopes the loop to implement, so plan/test run
    normally and the failure comes from the implement phase itself.
    """
    # See test_implement_retries_once_then_succeeds: import-time binding of the
    # MODEL_PRIMARY/MODEL_FALLBACK constants requires direct module attribute patching.
    monkeypatch.setattr(config, "MODEL_PRIMARY", config.ModelConfig("verboo/deepseek-v4-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK", config.ModelConfig("verboo/glm-4.7-flash", "high"))
    monkeypatch.setattr(config, "MODEL_FALLBACK_ENABLED", True)
    monkeypatch.setenv("FAKE_OPCODE_LOOP", "1")
    monkeypatch.setenv("FAKE_OPCODE_LOOP_PROMPT", "Implement work item")
    monkeypatch.setattr("orchestrator.github.create_pull_request", lambda *a, **k: 42)
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda *a, **k: None)
    graph = build_graph(store.checkpointer())
    result = graph.invoke(_seed(remote_repo, 11), config={"configurable": {"thread_id": "company/backend#11"}})
    assert result["status"] == state_mod.FAILED
    assert "degenerate output" in result["error"]
    assert result["phase_attempts"] == config.PHASE_MAX_ATTEMPTS
