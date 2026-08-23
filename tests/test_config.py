"""Tests for model configuration parsing and env overrides."""

from __future__ import annotations

import os
from pathlib import Path

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
