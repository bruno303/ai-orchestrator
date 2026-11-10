"""CLI stateless behavior."""

import json
from types import SimpleNamespace

import pytest

from orchestrator.application.execution.agent import AgentSettings
from orchestrator.infra.filesystem.command import run_workspace_command
from orchestrator.infra.github import client as github
from orchestrator.infra.langgraph import state as state_mod
from orchestrator.infra.langgraph.execution_steps import ExecutionStageDeps
from orchestrator.application.workflow import StepSpec, default_steps
from orchestrator.main import cli
from orchestrator.main import composition
from orchestrator.main import config
from orchestrator.main.cli import _poll_reviews, main
from orchestrator.main.composition import compose_execution_workflow
from test_graph import CommandRuntime, FakeRuntime  # sibling test module; both fake ExecutionRuntime


def _seed(**data) -> dict:
    """Minimal graph input seed matching the built-in node contract."""
    payload = {
        "id": "company/backend#1",
        "repository": "company/backend",
        "title": "Add feature",
        "description": "Implement it",
        **data,
    }
    return {"input": {"provider": "fake", "data": payload}}


def test_removed_persistence_commands_are_absent(capsys):
    with pytest.raises(SystemExit): main(["list"])
    assert "invalid choice" in capsys.readouterr().err


def test_run_skips_developed_issue_without_force(allowlist, monkeypatch):
    monkeypatch.setattr(github, "get_issue", lambda *args: github.Issue(1, "t", "b", "u", ["ai-developed"]))
    with pytest.raises(SystemExit, match="already labeled ai-developed"):
        main(["run", "company/backend#1"])


def test_run_uses_the_configured_input_client_for_issue_metadata(allowlist, monkeypatch):
    calls = []

    class Client:
        GitHubError = github.GitHubError

        def get_issue(self, repository, number):
            calls.append(("issue", repository, number))
            return github.Issue(number, "title", "body", "url", [])

        def get_repository(self, repository):
            calls.append(("repository", repository))
            return {"clone_url": "https://github.com/company/backend.git", "default_branch": "main"}

        @staticmethod
        def https_clone_url(metadata, repository):
            return metadata["clone_url"]

    runtime = SimpleNamespace(
        input_source=SimpleNamespace(github_client=Client()), executor=object(),
        workspace_manager=object(), destination=object(), execution_runtime=object(),
    )
    monkeypatch.setattr(cli, "compose_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "compose_execution_workflow",
                        lambda runtime: ([], SimpleNamespace(runtime=runtime)))
    monkeypatch.setattr(cli, "_run_graph", lambda seed, task_id, **kwargs: {"task_id": task_id, "status": "completed"})

    cli.cmd_run(SimpleNamespace(issue_ref="company/backend#8", force=False))

    assert calls == [("issue", "company/backend", 8), ("issue", "company/backend", 8), ("repository", "company/backend")]


def test_reset_clears_issue_assignee_before_marker(allowlist, monkeypatch):
    calls = []

    class Client:
        GitHubError = github.GitHubError

        def get_issue(self, repository, number):
            calls.append(("get_issue", repository, number))
            return github.Issue(number, "title", "body", "url", [])

        def get_authenticated_user_login(self):
            calls.append(("get_login",))
            return "app/bruno303-ai-agent-bot"

        def _run_gh(self, args, input_text=None):
            calls.append(("run_gh", args, json.loads(input_text)))
            return ""

        def remove_issue_label(self, repository, number, label):
            calls.append(("remove_label", repository, number, label))

    destination = SimpleNamespace(github_client=Client())
    monkeypatch.setattr(cli, "compose_execution_runtime", lambda: SimpleNamespace(destination=destination))

    cli.cmd_reset(SimpleNamespace(issue_ref="company/backend#8"))

    assert calls == [
        ("get_issue", "company/backend", 8),
        ("get_login",),
        (
            "run_gh",
            [
                "api", "--method", "DELETE",
                "repos/company/backend/issues/8/assignees", "--input", "-",
            ],
            {"assignees": ["app/bruno303-ai-agent-bot"]},
        ),
        ("remove_label", "company/backend", 8, "ai-developed"),
    ]


def test_reset_does_nothing_for_pull_requests(allowlist, monkeypatch):
    calls = []

    class Client:
        GitHubError = github.GitHubError

        def get_issue(self, repository, number):
            calls.append(("get_issue", repository, number))
            raise github.GitHubError(f"#{number} in {repository} is a pull request, not an issue")

        def get_authenticated_user_login(self):
            raise AssertionError("PR reset must not resolve an assignee")

        def _run_gh(self, args, input_text=None):
            raise AssertionError("PR reset must not remove assignees")

        def remove_issue_label(self, repository, number, label):
            calls.append(("remove_label", repository, number, label))

    destination = SimpleNamespace(github_client=Client())
    monkeypatch.setattr(cli, "compose_execution_runtime", lambda: SimpleNamespace(destination=destination))

    cli.cmd_reset(SimpleNamespace(issue_ref="company/backend#8"))

    assert calls == [("get_issue", "company/backend", 8)]


def test_review_poll_logs_completion_after_success(capsys):
    _poll_reviews(type("Reviews", (), {"poll_once": lambda self: None})())

    assert "review poll: finished" in capsys.readouterr().out


def test_review_poll_logs_completion_after_error(capsys):
    def fail(self):
        raise RuntimeError("source unavailable")

    _poll_reviews(type("Reviews", (), {"poll_once": fail})())

    output = capsys.readouterr().out
    assert "review poll error (continuing): source unavailable" in output
    assert "review poll: finished" in output


def test_review_logs_wait_before_next_poll(monkeypatch, capsys):
    closed = []
    monkeypatch.setattr(cli, "_acquire_poll_lock", lambda: type("Lock", (), {"close": lambda self: closed.append(True)})())
    monkeypatch.setattr(cli, "compose_review_runtime", lambda: object())
    monkeypatch.setattr(cli, "_poll_reviews", lambda _reviews: None)
    monkeypatch.setattr(cli.config, "POLL_INTERVAL_SECONDS", 42)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: (_ for _ in ()).throw(RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):
        cli.cmd_review(SimpleNamespace(once=False))

    assert "review: next check in 42s" in capsys.readouterr().out
    assert closed == [True]


def test_triage_uses_a_separate_poll_lock(monkeypatch):
    lock_names = []

    class Lock:
        def close(self):
            pass

    monkeypatch.setattr(cli, "_acquire_poll_lock", lambda name="poll": lock_names.append(name) or Lock())
    monkeypatch.setattr(cli, "compose_triage_runtime", lambda: object())
    monkeypatch.setattr(cli, "_poll_triage", lambda _triage: None)

    cli.cmd_triage(SimpleNamespace(once=True))

    assert lock_names == ["triage"]


def test_execute_does_not_run_triage(monkeypatch):
    calls = []

    class Lock:
        def close(self):
            calls.append("close")

    class Application:
        def __init__(self, *args, **kwargs):
            pass

        def poll_once(self, once):
            calls.append("execute")

    runtime = SimpleNamespace(
        input_source=object(),
        executor=object(),
        workspace_manager=object(),
        destination=object(),
        execution_runtime=object(),
        input_provider="github_polling",
        feedback=None,
    )
    monkeypatch.setattr(cli, "_acquire_poll_lock", lambda: Lock())
    monkeypatch.setattr(cli, "compose_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "compose_review_runtime", lambda: object())
    monkeypatch.setattr(cli, "compose_triage_runtime", lambda: (_ for _ in ()).throw(
        AssertionError("execute must not compose triage")
    ))
    monkeypatch.setattr(cli, "PollingApplication", Application)
    monkeypatch.setattr(cli, "_poll_reviews", lambda _reviews: calls.append("review"))

    cli.cmd_execute(SimpleNamespace(once=True))

    assert calls == ["execute", "review", "close"]


def test_keyboard_interrupt_prints_generic_stop_message(monkeypatch, capsys):
    def interrupt(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_review", interrupt)

    with pytest.raises(SystemExit) as exc_info:
        main(["review", "--once"])

    assert exc_info.value.code == 130
    assert capsys.readouterr().out == "\nprocess stopped.\n"


# --- configurable execution workflow composition (Task 5) ---


def test_compose_execution_workflow_defaults_without_a_workflow_section(allowlist):
    config.load_pipeline_config.cache_clear()

    steps, deps = compose_execution_workflow()

    assert steps == list(default_steps("execution"))
    assert deps.step_runtimes == {}
    assert deps.prompts == {}
    assert deps.command_timeout_seconds == config.COMMAND_TIMEOUT_SECONDS
    assert deps.reuse_workspace is False


def test_compose_execution_workflow_uses_configured_steps(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: checkout, type: checkout}\n"
        "        - {name: plan, type: plan}\n"
        "        - {name: publish, type: publish}\n"
    )
    config.load_pipeline_config.cache_clear()

    steps, deps = compose_execution_workflow()

    assert [step.name for step in steps] == ["checkout", "plan", "publish"]
    assert [(step.name, step.type) for step in steps] == [
        ("checkout", "checkout"), ("plan", "plan"), ("publish", "publish"),
    ]
    assert deps.step_runtimes == {}  # no overrides: everyone reuses the default runtime


def test_compose_execution_workflow_builds_runtime_variants_only_for_overridden_steps(
    allowlist, monkeypatch
):
    real_compose = composition.compose_execution_runtime
    calls = []

    def recording_compose(**kwargs):
        calls.append(kwargs)
        return real_compose(**kwargs)

    monkeypatch.setattr(composition, "compose_execution_runtime", recording_compose)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    executor: {type: opencode, timeout: 30}\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: checkout, type: checkout}\n"
        "        - {name: plan, type: plan, model: {name: verboo/deepseek-v4-flash}}\n"
        "        - {name: implement, type: implement, executor: codex}\n"
    )
    config.load_pipeline_config.cache_clear()

    steps, deps = compose_execution_workflow()
    runtime = deps.runtime

    assert calls[0] == {}  # default runtime composed once, as today
    assert set(deps.step_runtimes) == {"plan", "implement"}
    # The checkout step has no overrides and reuses the default runtime.
    assert deps.runtime_for(steps[0]) is runtime is not deps.step_runtimes["plan"]

    plan_call = calls[1]
    assert plan_call["executor"] is runtime.agent.executor  # stage executor instance
    assert plan_call["workspace_manager"] is runtime.workspace_manager
    assert plan_call["destination"] is runtime.destination
    assert plan_call["agent_settings"] == AgentSettings(config.ModelConfig(
        "verboo/deepseek-v4-flash", None
    ))

    implement_call = calls[2]
    assert implement_call["workspace_manager"] is runtime.workspace_manager
    assert implement_call["destination"] is runtime.destination
    assert implement_call["agent_settings"] is None  # stage model config applies
    assert implement_call["executor"].options == {"timeout": 30}
    assert deps.step_runtimes["implement"].agent.executor is not runtime.agent.executor
    # The stage default runtime keeps the stage model config (absent here).
    assert runtime.agent.settings.model is None


def test_compose_execution_workflow_loads_prompt_files(allowlist, tmp_path, monkeypatch):
    prompt_file = tmp_path / "prompts" / "plan.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("Plan {task_id} in {repository}")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: plan, type: plan, prompt_file: prompts/plan.md}\n"
    )
    config.load_pipeline_config.cache_clear()

    steps, deps = compose_execution_workflow()

    assert deps.prompts == {"plan": "Plan {task_id} in {repository}"}
    assert deps.step_runtimes == {}


def test_compose_execution_workflow_fails_fast_on_a_missing_prompt_file(
    allowlist, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: plan, type: plan, prompt_file: prompts/missing.md}\n"
    )
    config.load_pipeline_config.cache_clear()

    with pytest.raises(ValueError, match="step 'plan'.*prompts/missing.md"):
        compose_execution_workflow()


def test_run_graph_executes_a_configured_step_chain(allowlist):
    fake = FakeRuntime()
    steps = [StepSpec("checkout", "checkout"), StepSpec("publish", "publish")]

    result = cli._run_graph(
        _seed(), "company/backend#11", runtime=fake, steps=steps,
        deps=ExecutionStageDeps(runtime=fake),
    )

    assert result["status"] == state_mod.COMPLETED
    assert fake.nodes == ["prepare_workspace", "create_pr"]
    assert result["step_results"]["publish"]["status"] == "succeeded"
    assert result["output"]["external_id"] == "17"


def test_run_graph_default_path_runs_the_default_five_node_sequence(allowlist):
    fake = FakeRuntime()

    result = cli._run_graph(_seed(), "company/backend#12", runtime=fake)

    assert result["status"] == state_mod.COMPLETED
    assert fake.nodes == ["prepare_workspace", "plan", "implement", "create_pr", "cleanup"]
    assert result["step_results"]["cleanup"]["status"] == "succeeded"


def test_compose_execution_workflow_reuses_a_provided_runtime(allowlist, monkeypatch):
    def fail_compose(**kwargs):
        raise AssertionError("a provided runtime must not be re-composed")

    monkeypatch.setattr(composition, "compose_execution_runtime", fail_compose)
    config.load_pipeline_config.cache_clear()
    runtime = object()

    steps, deps = compose_execution_workflow(runtime=runtime)

    assert deps.runtime is runtime
    assert steps == list(default_steps("execution"))


def test_execute_runs_the_configured_workflow_steps(allowlist, monkeypatch):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: checkout, type: checkout}\n"
        "        - {name: publish, type: publish}\n"
    )
    config.load_pipeline_config.cache_clear()

    fake = FakeRuntime()
    runtime = SimpleNamespace(
        input_source=object(), executor=object(), workspace_manager=object(),
        destination=object(), execution_runtime=fake, input_provider="github_polling",
        feedback=None,
    )
    monkeypatch.setattr(cli, "compose_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "compose_review_runtime", lambda: object())
    monkeypatch.setattr(cli, "_poll_reviews", lambda _reviews: None)
    monkeypatch.setattr(cli, "_acquire_poll_lock",
                        lambda: type("Lock", (), {"close": lambda self: None})())

    run_graphs = []
    results = []

    class Application:
        def __init__(self, source, run_graph, report_result, remove_event_workspace, **kwargs):
            run_graphs.append(run_graph)

        def poll_once(self, once):
            results.append(run_graphs[0](_seed(), "company/backend#21"))

    monkeypatch.setattr(cli, "PollingApplication", Application)

    cli.cmd_execute(SimpleNamespace(once=True))

    assert results[0]["status"] == state_mod.COMPLETED
    assert results[0]["output"]["external_id"] == "17"
    assert fake.nodes == ["prepare_workspace", "create_pr"]  # exactly the configured steps


def test_compose_execution_runtime_wires_the_workspace_command_runner(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: scripts, type: command, command: ./scripts/verify.sh}\n"
        "        - {name: implement, type: implement, executor: claude}\n"
    )
    config.load_pipeline_config.cache_clear()

    steps, deps = compose_execution_workflow()

    # `command` steps click through the composed runtimes: the default runtime
    # and every per-step override variant carry the real workspace adapter.
    assert deps.runtime.command_runner is run_workspace_command
    assert deps.step_runtimes["implement"].command_runner is run_workspace_command


def test_run_graph_records_a_node_end_for_every_streamed_step(allowlist, monkeypatch):
    events = []
    monkeypatch.setattr(cli.workspace, "append_event", lambda _task_id, **fields: events.append(fields))

    fake = FakeRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("lint", "command", command="ruff check ."),  # success, no state-level status
        StepSpec("tests", "command", command="pytest -q", on_failure="continue"),  # failure, popped status
        StepSpec("publish", "publish"),
    ]
    deps = ExecutionStageDeps(
        runtime=fake,
        step_runtimes={"lint": CommandRuntime(0, stdout="ok"),
                       "tests": CommandRuntime(2, stderr="boom")},
    )

    result = cli._run_graph(_seed(), "company/backend#13", runtime=fake, steps=steps, deps=deps)

    assert result["status"] == state_mod.COMPLETED  # the continued failure stays non-fatal
    node_ends = [event for event in events if event.get("event") == "node_end"]
    recorded = {event.get("node"): event for event in node_ends}
    assert set(recorded) == {"checkout", "lint", "tests", "publish"}
    assert recorded["checkout"]["status"] == state_mod.PREPARING
    assert recorded["publish"]["status"] == state_mod.COMPLETED
    assert recorded["lint"]["status"] == "succeeded"
    assert recorded["tests"]["status"] == "failed"
    assert "exit 2" in recorded["tests"]["error"]
    assert recorded["tests"]["duration_s"] >= 0
