"""Composition of comment, discussion, review, and triage workflow overrides."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator.application.review.service import ReviewRuntime
from orchestrator.application.workflow import StepSpec, default_steps
from orchestrator.infra.filesystem.command import run_workspace_command
from orchestrator.main import composition, config


def _clear_config():
    config.load_pipeline_config.cache_clear()


def test_compose_comment_impl_workflow_uses_configured_steps(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    comments:\n"
        "      impl:\n"
        "        workflow:\n"
        "          steps:\n"
        "            - {name: checkout, type: checkout}\n"
        "            - {name: implement, type: implement_comment}\n"
        "            - {name: publish, type: publish}\n"
    )
    _clear_config()
    runtime = object()

    steps, deps = composition.compose_comment_impl_workflow(runtime=runtime)

    assert steps == [
        StepSpec("checkout", "checkout"),
        StepSpec("implement", "implement_comment"),
        StepSpec("publish", "publish"),
    ]
    assert deps.runtime is runtime
    assert deps.reuse_workspace is True


def test_compose_comment_impl_workflow_loads_prompt_files(
    allowlist, tmp_path, monkeypatch
):
    prompt_file = tmp_path / "prompts" / "comment.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("custom comment prompt")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    comments:\n"
        "      impl:\n"
        "        workflow:\n"
        "          steps:\n"
        "            - {name: checkout, type: checkout}\n"
        "            - {name: implement, type: implement_comment, prompt_file: prompts/comment.md}\n"
    )
    _clear_config()

    steps, deps = composition.compose_comment_impl_workflow(runtime=object())

    assert steps[1].prompt_file == "prompts/comment.md"
    assert deps.prompts == {"implement": "custom comment prompt"}


def test_compose_comment_impl_defaults_to_the_built_in_sequence(allowlist):
    _clear_config()

    steps, deps = composition.compose_comment_impl_workflow(runtime=object())

    assert steps == list(default_steps("comment_impl"))
    assert deps.reuse_workspace is True


def test_compose_discussion_workflow_loads_prompts_and_executor_overrides(
    allowlist, tmp_path, monkeypatch
):
    prompt_file = tmp_path / "prompts" / "discuss.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("answer as {task_id}")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    comments:\n"
        "      discuss:\n"
        "        workflow:\n"
        "          steps:\n"
        "            - {name: checkout, type: checkout}\n"
        "            - name: answer\n"
        "              type: discuss\n"
        "              executor: claude\n"
        "              model: {name: provider/discuss}\n"
        "              prompt_file: prompts/discuss.md\n"
        "            - {name: publish, type: publish}\n"
    )
    _clear_config()

    created = []

    def fake_create(registry, provider, *, overrides=None):
        created.append((registry, provider, overrides))
        return object()

    monkeypatch.setattr(composition, "_create", fake_create)
    runtime = SimpleNamespace(
        workspace_manager=object(),
        destination=object(),
        repository_allowed=lambda repository: True,
        task_log_path=lambda task_id, node: Path("/tmp/log"),
    )

    steps, deps = composition.compose_discussion_workflow(runtime=runtime)

    assert [step.name for step in steps] == ["checkout", "answer", "publish"]
    assert deps.prompts == {"answer": "answer as {task_id}"}
    assert set(deps.step_runtimes) == {"answer"}
    override = deps.step_runtimes["answer"]
    assert override.workspace_manager is runtime.workspace_manager
    assert override.destination is runtime.destination
    assert override.model_config == config.ModelConfig("provider/discuss", None)
    assert created[0][1].type == "claude"
    assert created[0][2] == {"model_config": config.ModelConfig("provider/discuss", None)}


def test_compose_review_workflow_builds_per_step_runtime_and_prompts(
    allowlist, tmp_path, monkeypatch
):
    prompt_file = tmp_path / "prompts" / "review.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("custom review")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  review:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: checkout, type: checkout}\n"
        "        - name: review\n"
        "          type: review\n"
        "          model: {name: provider/review, variant: low}\n"
        "          prompt_file: prompts/review.md\n"
        "        - {name: publish, type: publish}\n"
    )
    _clear_config()

    created = []

    def fake_create(registry, provider, *, overrides=None):
        created.append((registry, provider, overrides))
        return object()

    monkeypatch.setattr(composition, "_create", fake_create)
    runtime = ReviewRuntime(
        object(), object(), object(), task_log_path=lambda task_id, node: Path("/tmp/log"),
    )

    steps, deps = composition.compose_review_workflow(runtime=runtime)

    assert [step.name for step in steps] == ["checkout", "review", "publish"]
    assert deps.prompts == {"review": "custom review"}
    assert set(deps.step_runtimes) == {"review"}
    assert deps.step_runtimes["review"].workspace_manager is runtime.workspace_manager
    assert deps.step_runtimes["review"].destination is runtime.destination
    assert created[0][1].type == "opencode"
    assert created[0][2] == {
        "model_config": config.ModelConfig("provider/review", "low")
    }
    assert deps.command_runner is run_workspace_command


def test_compose_triage_workflow_builds_per_step_executor_and_prompts(
    allowlist, tmp_path, monkeypatch
):
    prompt_file = tmp_path / "prompts" / "triage.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("custom triage")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  triage:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - name: assess\n"
        "          type: assess\n"
        "          executor: claude\n"
        "          model: {name: provider/triage}\n"
        "          prompt_file: prompts/triage.md\n"
        "        - {name: publish, type: publish}\n"
    )
    _clear_config()

    created = []

    def fake_create(registry, provider, *, overrides=None):
        created.append((registry, provider, overrides))
        return object()

    monkeypatch.setattr(composition, "_create", fake_create)
    executor = object()
    destination = object()

    steps, deps = composition.compose_triage_workflow(
        executor=executor, destination=destination,
    )

    assert [step.name for step in steps] == ["assess", "publish"]
    assert deps.prompts == {"assess": "custom triage"}
    assert deps.executor is executor
    assert deps.destination is destination
    assert set(deps.step_executors) == {"assess"}
    assert created[0][1].type == "claude"
    assert created[0][2] == {
        "model_config": config.ModelConfig("provider/triage", None)
    }
