"""Tests for model configuration parsing and env overrides."""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.application.workflow import StepConfigError, StepSpec, default_steps
from orchestrator.main import config


def test_environment_path_expands_home_directory(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_REPOS_DIR", "~/agent-repos")

    assert config._environment_path("ORCHESTRATOR_REPOS_DIR", "/unused") == Path.home() / "agent-repos"


def test_load_environment_reads_env_file_without_overwriting_exported_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ORCHESTRATOR_POLL_INTERVAL=17\nORCHESTRATOR_OPENCODE_BIN=from-env\n")
    monkeypatch.setenv("ORCHESTRATOR_LOAD_DOTENV", "1")
    monkeypatch.setenv("ORCHESTRATOR_OPENCODE_BIN", "exported-value")
    monkeypatch.delenv("ORCHESTRATOR_POLL_INTERVAL", raising=False)
    monkeypatch.setattr(config, "ENV_FILE", env_file)

    assert config.load_environment()
    assert os.environ["ORCHESTRATOR_POLL_INTERVAL"] == "17"
    assert os.environ["ORCHESTRATOR_OPENCODE_BIN"] == "exported-value"


def test_load_environment_is_disabled_for_tests(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ORCHESTRATOR_POLL_INTERVAL=17\n")
    monkeypatch.setenv("ORCHESTRATOR_LOAD_DOTENV", "0")
    monkeypatch.delenv("ORCHESTRATOR_POLL_INTERVAL", raising=False)
    monkeypatch.setattr(config, "ENV_FILE", env_file)

    assert not config.load_environment()
    assert "ORCHESTRATOR_POLL_INTERVAL" not in os.environ


def _clear_pipeline_cache():
    config.load_pipeline_config.cache_clear()
    config.load_review_pipeline_config.cache_clear()


def test_execution_model_config_parses(model_config):
    model = config.load_execution_model_config()
    assert model.name == "verboo/deepseek-v4-flash"
    assert model.variant == "high"


def test_review_model_config_parses_independently(model_config):
    model = config.load_review_model_config()
    assert model.name == "openai/gpt-5.6-luna"
    assert model.variant == "medium"


def test_triage_model_config_parses_independently(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "model:\n  triage:\n    name: provider/triage\n    variant: low\n"
    )
    config.load_triage_model_config.cache_clear()
    assert config.load_triage_model_config() == config.ModelConfig("provider/triage", "low")


def test_model_config_sections_do_not_inherit(allowlist):
    config.CONFIG_FILE.write_text(
        "model:\n"
        "  execution:\n"
        "    name: provider/execution\n"
        "    variant: high\n"
    )
    config.load_execution_model_config.cache_clear()
    config.load_review_model_config.cache_clear()
    execution = config.load_execution_model_config()
    assert execution == config.ModelConfig("provider/execution", "high")
    assert config.load_review_model_config() is None


def test_model_config_environment_overrides_are_scoped(model_config, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_MODEL_EXECUTION_NAME", "provider/execution-override")
    monkeypatch.setenv("ORCHESTRATOR_MODEL_EXECUTION_VARIANT", "low")
    monkeypatch.setenv("ORCHESTRATOR_MODEL_REVIEW_NAME", "provider/review-override")
    monkeypatch.setenv("ORCHESTRATOR_MODEL_REVIEW_VARIANT", "high")
    config.load_execution_model_config.cache_clear()
    config.load_review_model_config.cache_clear()

    assert config.load_execution_model_config() == config.ModelConfig("provider/execution-override", "low")
    assert config.load_review_model_config() == config.ModelConfig("provider/review-override", "high")


def test_model_config_absent(allowlist):
    assert config.load_execution_model_config() is None
    assert config.load_review_model_config() is None


def test_pipeline_config_defaults(allowlist):
    _clear_pipeline_cache()
    pipeline = config.load_pipeline_config()
    assert pipeline.execution.input_source.type == "github_polling"
    assert pipeline.execution.executor.type == "opencode"
    assert pipeline.execution.workspace_manager.type == "git"
    assert pipeline.execution.destination.type == "github"
    assert pipeline.review.workspace_manager.type == "git"
    assert pipeline.review.workspace_manager.options == {}
    assert pipeline.triage.input_source.type == "github_polling"
    assert pipeline.triage.executor.type == "opencode"
    assert pipeline.triage.destination.type == "github"
    assert pipeline.execution.labels.select == ("ai-agent",)
    assert pipeline.execution.labels.suppress == ("ai-developed",)
    assert pipeline.execution.labels.output.add == ("ai-developed",)
    assert pipeline.review.labels.suppress == ("ai-reviewed",)
    assert pipeline.review.labels.output.add == ("ai-reviewed",)
    assert pipeline.triage.labels.suppress == ("ai-agent", "ai-triage", "ai-developed")
    assert pipeline.triage.labels.output.ready.add == ("ai-agent",)
    assert pipeline.triage.labels.output.ready.remove == ("ai-triage",)
    assert pipeline.triage.labels.output.blocked.add == ("ai-triage",)


def test_logging_verbosity_defaults_to_normal(allowlist):
    config.load_logging_config.cache_clear()
    assert config.load_logging_config().verbosity == "normal"


def test_logging_verbosity_is_validated(allowlist):
    config.CONFIG_FILE.write_text("logging:\n  verbosity: noisy\n")
    config.load_logging_config.cache_clear()
    try:
        config.load_logging_config()
    except ValueError as exc:
        assert "logging.verbosity" in str(exc)
        assert "quiet, normal, verbose" in str(exc)
    else:
        raise AssertionError("expected invalid logging verbosity error")


def test_stage_label_contracts_are_translated_to_provider_options(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  triage:\n"
        "    labels:\n"
        "      select: []\n"
        "      suppress: [ready, blocked, developed]\n"
        "      output:\n"
        "        ready: {add: [ready], remove: [blocked]}\n"
        "        blocked: {add: [blocked]}\n"
    )
    _clear_pipeline_cache()

    from orchestrator.main.composition import compose_triage_runtime

    runtime = compose_triage_runtime()

    assert runtime.input_source.options["select_labels"] == []
    assert runtime.input_source.options["suppress_labels"] == ["ready", "blocked", "developed"]
    assert runtime.destination.options["ready_output"] == {
        "add": ["ready"], "remove": ["blocked"],
    }
    assert runtime.destination.options["blocked_output"] == {
        "add": ["blocked"], "remove": [],
    }


def test_legacy_ai_agent_repository_label_is_a_noop(allowlist):
    config.CONFIG_FILE.write_text("repositories:\n  - name: company/backend\n    label: ai-agent\n")

    assert config.load_repository_config() == {"company/backend": {}}


def test_nonstandard_legacy_repository_label_is_rejected(allowlist):
    config.CONFIG_FILE.write_text("repositories:\n  - name: company/backend\n    label: ready-for-dev\n")

    try:
        config.load_repository_config()
    except ValueError as exc:
        assert "repository 'company/backend'" in str(exc)
        assert "label: 'ai-agent'" in str(exc)
        assert "pipeline.*.labels" in str(exc)
    else:
        raise AssertionError("expected unsupported repository label error")


def test_omitted_workspace_auth_remains_bot_compatible(allowlist):
    from orchestrator.main.composition import compose_runtime

    runtime = compose_runtime()

    assert runtime.workspace_manager.git_client.identity.mode == "bot"
    assert runtime.destination.git_client is runtime.workspace_manager.git_client


def test_pipeline_config_parses_provider_options(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    input_source:\n"
        "      type: github_polling\n"
        "      interval: 30\n"
        "    executor: opencode\n"
        "    workspace_manager:\n"
        "      type: git\n"
        "    destination:\n"
        "      type: github\n"
    )
    _clear_pipeline_cache()
    pipeline = config.load_pipeline_config()
    assert pipeline.execution.input_source.options == {"interval": 30}
    assert pipeline.execution.executor.options == {}
    assert pipeline.review.workspace_manager.options == {}


def test_pipeline_config_sections_do_not_inherit_provider_options(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    executor:\n"
        "      type: opencode\n"
        "      timeout: 30\n"
    )
    _clear_pipeline_cache()
    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.options == {"timeout": 30}
    assert pipeline.review.executor.options == {}


def test_review_pipeline_config_preserves_and_overrides_workspace_options(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workspace_manager:\n"
        "      type: git\n"
        "      root: /tmp/execution\n"
        "  review:\n"
        "    workspace_manager:\n"
        "      type: git\n"
        "      root: /tmp/reviews\n"
    )
    _clear_pipeline_cache()
    review = config.load_review_pipeline_config()
    assert review.workspace_manager.type == "git"
    assert review.workspace_manager.options == {"root": "/tmp/reviews"}


def test_clearing_pipeline_cache_refreshes_review_config(allowlist):
    config.CONFIG_FILE.write_text("pipeline:\n  review:\n    workspace_manager:\n      type: git\n      root: /tmp/old\n")
    _clear_pipeline_cache()
    assert config.load_review_pipeline_config().workspace_manager.options == {"root": "/tmp/old"}
    config.CONFIG_FILE.write_text("pipeline:\n  review:\n    workspace_manager:\n      type: git\n      root: /tmp/new\n")
    config.load_pipeline_config.cache_clear()
    assert config.load_review_pipeline_config().workspace_manager.options == {"root": "/tmp/new"}


def test_pipeline_config_rejects_unknown_provider(allowlist):
    config.CONFIG_FILE.write_text("pipeline:\n  execution:\n    executor:\n      type: unknown\n")
    _clear_pipeline_cache()
    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "unknown provider type: unknown" in str(exc)
    else:
        raise AssertionError("expected unknown provider error")


def test_pipeline_config_accepts_codex_for_execution_and_review(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    executor: {type: codex, sandbox: workspace-write}\n"
        "  review:\n"
        "    executor: {type: codex, sandbox: read-only}\n"
    )
    _clear_pipeline_cache()

    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.type == "codex"
    assert pipeline.execution.executor.options == {"sandbox": "workspace-write"}
    assert pipeline.review.executor.type == "codex"
    assert pipeline.review.executor.options == {"sandbox": "read-only"}


def test_pipeline_config_accepts_claude_for_execution_and_review(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    executor: {type: claude, permission_mode: acceptEdits}\n"
        "  review:\n"
        "    executor: {type: claude, permission_mode: plan}\n"
    )
    _clear_pipeline_cache()

    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.type == "claude"
    assert pipeline.execution.executor.options == {"permission_mode": "acceptEdits"}
    assert pipeline.review.executor.type == "claude"
    assert pipeline.review.executor.options == {"permission_mode": "plan"}


def test_pipeline_executor_environment_overrides_are_scoped_and_preserve_options(allowlist, monkeypatch):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    executor: {type: opencode, sandbox: workspace-write}\n"
        "  review:\n"
        "    executor: {type: codex, sandbox: read-only}\n"
    )
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_EXECUTION", "codex")
    _clear_pipeline_cache()

    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.type == "codex"
    assert pipeline.execution.executor.options == {"sandbox": "workspace-write"}
    assert pipeline.review.executor.type == "codex"
    assert pipeline.review.executor.options == {"sandbox": "read-only"}


def test_pipeline_executor_environment_overrides_can_select_each_workflow(allowlist, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_EXECUTION", "codex")
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_REVIEW", "opencode")
    _clear_pipeline_cache()

    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.type == "codex"
    assert pipeline.review.executor.type == "opencode"


def test_pipeline_triage_executor_environment_override(allowlist, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_TRIAGE", "codex")
    _clear_pipeline_cache()

    assert config.load_pipeline_config().triage.executor.type == "codex"


def test_pipeline_executor_environment_overrides_can_select_claude(allowlist, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_EXECUTION", "claude")
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_REVIEW", "claude")
    _clear_pipeline_cache()

    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.type == "claude"
    assert pipeline.review.executor.type == "claude"


def test_pipeline_executor_environment_override_rejects_unknown_provider(allowlist, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_EXECUTOR_EXECUTION", "unknown")
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "unknown provider type: unknown" in str(exc)
    else:
        raise AssertionError("expected unknown provider error")


def test_pipeline_config_workflow_defaults_match_built_in_steps(allowlist):
    _clear_pipeline_cache()
    pipeline = config.load_pipeline_config()
    assert pipeline.execution.workflow == default_steps("execution")
    assert pipeline.execution.comment_impl_workflow == default_steps("comment_impl")
    assert pipeline.execution.comment_discuss_workflow == default_steps("discuss")
    assert pipeline.review.workflow == default_steps("review")
    assert pipeline.triage.workflow == default_steps("triage")


def test_pipeline_config_empty_workflow_section_falls_back_to_defaults(allowlist):
    config.CONFIG_FILE.write_text("pipeline:\n  review:\n    workflow: {}\n")
    _clear_pipeline_cache()

    assert config.load_pipeline_config().review.workflow == default_steps("review")


def test_pipeline_config_parses_workflow_steps_into_step_specs(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: checkout, type: checkout}\n"
        "        - {name: plan, type: plan, model: {name: verboo/deepseek-v4-flash, variant: high}}\n"
        "        - {name: document, type: agent, executor: claude, prompt_file: prompts/document.md, agent: writer}\n"
        "        - {name: tests-script, type: command, command: ./scripts/verify.sh, timeout_seconds: 900}\n"
        "        - {name: cleanup, type: cleanup, on_failure: continue}\n"
    )
    _clear_pipeline_cache()

    workflow = config.load_pipeline_config().execution.workflow

    assert all(isinstance(step, StepSpec) for step in workflow)
    assert [step.name for step in workflow] == ["checkout", "plan", "document", "tests-script", "cleanup"]
    assert workflow[1].model_name == "verboo/deepseek-v4-flash"
    assert workflow[1].model_variant == "high"
    assert workflow[2].executor_type == "claude"
    assert workflow[2].prompt_file == "prompts/document.md"
    assert workflow[2].agent == "writer"
    assert workflow[3].command == "./scripts/verify.sh"
    assert workflow[3].timeout_seconds == 900
    assert workflow[4].on_failure == "continue"
    assert all(step.on_failure == "abort" for step in workflow[:4])


def test_pipeline_config_accepts_bare_step_lists_and_both_executor_forms(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      - {name: checkout, type: checkout}\n"
        "      - {name: implement, type: implement, executor: claude}\n"
        "      - {name: codex-task, type: agent, executor: {type: codex}, prompt_file: prompts/faq.md}\n"
    )
    _clear_pipeline_cache()

    workflow = config.load_pipeline_config().execution.workflow

    assert workflow[1].executor_type == "claude"
    assert workflow[2].executor_type == "codex"


def test_pipeline_config_parses_comment_workflows_independently(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    comments:\n"
        "      impl:\n"
        "        workflow:\n"
        "          steps:\n"
        "            - {name: implement, type: implement_comment}\n"
        "      discuss:\n"
        "        workflow:\n"
        "          steps:\n"
        "            - {name: answer, type: discuss}\n"
    )
    _clear_pipeline_cache()

    execution = config.load_pipeline_config().execution

    assert execution.comment_impl_workflow == (StepSpec("implement", "implement_comment"),)
    assert execution.comment_discuss_workflow == (StepSpec("answer", "discuss"),)


def test_pipeline_config_comment_workflow_on_one_path_keeps_the_other_default(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    comments:\n"
        "      impl:\n"
        "        workflow:\n"
        "          steps:\n"
        "            - {name: implement, type: implement_comment}\n"
    )
    _clear_pipeline_cache()

    execution = config.load_pipeline_config().execution

    assert execution.comment_impl_workflow == (StepSpec("implement", "implement_comment"),)
    assert execution.comment_discuss_workflow == default_steps("discuss")


def test_pipeline_config_rejects_unknown_workflow_step_type(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: bogus, type: bogus}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except StepConfigError as exc:
        assert "execution step 'bogus': unknown step type 'bogus'" in str(exc)
    else:
        raise AssertionError("expected unknown step type error")


def test_pipeline_config_rejects_duplicate_workflow_step_names(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: implement, type: implement}\n"
        "        - {name: implement, type: publish}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except StepConfigError as exc:
        assert "execution step 'implement': duplicate step name" in str(exc)
    else:
        raise AssertionError("expected duplicate step name error")


def test_pipeline_config_rejects_uppercase_workflow_step_names(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: Plan, type: plan}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except StepConfigError as exc:
        assert "execution step 'Plan': invalid step name" in str(exc)
    else:
        raise AssertionError("expected invalid step name error")


def test_pipeline_config_rejects_unknown_workflow_step_keys(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: checkout, type: checkout, retry: 3}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps[0] 'checkout'" in str(exc)
        assert "unknown step key(s) retry" in str(exc)
    else:
        raise AssertionError("expected unknown step key error")


def test_pipeline_config_rejects_non_string_workflow_step_keys(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: faq, type: plan, 1: oops}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps[0] 'faq'" in str(exc)
        assert "unknown step key(s) 1" in str(exc)
    else:
        raise AssertionError("expected unknown step key error")


def test_pipeline_config_rejects_empty_workflow_step_lists(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps: []\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps" in str(exc)
        assert "at least one step" in str(exc)
    else:
        raise AssertionError("expected empty steps list error")


def test_pipeline_config_rejects_non_mapping_workflow_steps(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - checkout\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps[0] must be a step mapping" in str(exc)
        assert "got str" in str(exc)
    else:
        raise AssertionError("expected non-mapping step entry error")


def test_pipeline_config_rejects_unknown_workflow_executor(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: faq, type: agent, executor: {type: bogus}}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps[0] 'faq'" in str(exc)
        assert "unknown executor provider type: bogus" in str(exc)
    else:
        raise AssertionError("expected unknown executor provider error")


def test_pipeline_config_rejects_agent_steps_without_prompt_file(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: faq, type: agent}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except StepConfigError as exc:
        assert "execution step 'faq': 'prompt_file' is required for agent steps" in str(exc)
    else:
        raise AssertionError("expected missing agent prompt_file error")


def test_pipeline_config_rejects_non_string_workflow_model_fields(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: plan, type: plan, model: {name: 3}}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps[0] 'plan'" in str(exc)
        assert "'model.name' must be a string" in str(exc)
    else:
        raise AssertionError("expected non-string model field error")


def test_pipeline_config_rejects_non_positive_workflow_timeouts(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  execution:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: tests-script, type: command, command: ./scripts/verify.sh, timeout_seconds: 0}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.execution.workflow.steps[0] 'tests-script'" in str(exc)
        assert "'timeout_seconds' must be a positive integer" in str(exc)
    else:
        raise AssertionError("expected non-positive timeout error")


def test_pipeline_config_rejects_review_workflow_executor_outside_review_registry(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  review:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: review, type: review, executor: {type: bogus}}\n"
    )
    _clear_pipeline_cache()

    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "pipeline.review.workflow.steps[0] 'review'" in str(exc)
        assert "unknown executor provider type: bogus" in str(exc)
    else:
        raise AssertionError("expected unknown review executor error")


def test_pipeline_config_accepts_review_workflow_executor_from_review_registry(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  review:\n"
        "    workflow:\n"
        "      steps:\n"
        "        - {name: review, type: review, executor: opencode}\n"
    )
    _clear_pipeline_cache()

    workflow = config.load_pipeline_config().review.workflow

    assert workflow == (StepSpec("review", "review", executor_type="opencode"),)
