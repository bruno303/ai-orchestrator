"""Tests for model configuration parsing and env overrides."""

from __future__ import annotations

from orchestrator.main import config


def _clear_pipeline_cache():
    config.load_pipeline_config.cache_clear()
    config.load_review_pipeline_config.cache_clear()


def test_model_config_parses(model_config):
    assert config.load_model_config()["primary"].name == "verboo/deepseek-v4-flash"
    assert config.load_model_config()["primary"].variant == "high"
    assert config.load_model_config()["fallback"].name == "verboo/glm-4.7-flash"
    assert config.load_model_config()["fallback"].variant == "high"


def test_model_config_absent(allowlist):
    assert config.load_model_config()["primary"] is None
    assert config.load_model_config()["fallback"] is None


def test_model_config_env_override(model_config, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_MODEL_FALLBACK_NAME", "verboo/some-other-model")
    monkeypatch.setenv("ORCHESTRATOR_MODEL_FALLBACK_VARIANT", "low")
    config.load_model_config.cache_clear()
    fallback = config.load_model_config()["fallback"]
    assert fallback.name == "verboo/some-other-model"
    assert fallback.variant == "low"


def test_model_fallback_disabled_by_default(allowlist):
    assert config.MODEL_FALLBACK_ENABLED is False


def test_model_fallback_enabled_from_config(model_config):
    assert config.load_fallback_enabled() is True


def test_model_fallback_enabled_env_override(allowlist, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_MODEL_FALLBACK_ENABLED", "true")
    assert config.load_fallback_enabled() is True


def test_pipeline_config_defaults(allowlist):
    _clear_pipeline_cache()
    pipeline = config.load_pipeline_config()
    assert pipeline.input_source.type == "github_polling"
    assert pipeline.executor.type == "opencode"
    assert pipeline.workspace_manager.type == "git"
    assert pipeline.destination.type == "github"
    assert pipeline.review.workspace_manager.type == "git"
    assert pipeline.review.workspace_manager.options == {}


def test_pipeline_config_parses_provider_options(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  input_source:\n"
        "    type: github_polling\n"
        "    interval: 30\n"
        "  executor: opencode\n"
        "  workspace:\n"
        "    type: git\n"
        "  destination:\n"
        "    type: github\n"
    )
    _clear_pipeline_cache()
    pipeline = config.load_pipeline_config()
    assert pipeline.input_source.options == {"interval": 30}
    assert pipeline.executor.options == {}
    assert pipeline.review.workspace_manager.options == {}


def test_review_pipeline_config_preserves_and_overrides_workspace_options(allowlist):
    config.CONFIG_FILE.write_text(
        "pipeline:\n"
        "  workspace:\n"
        "    type: git\n"
        "    root: /tmp/shared\n"
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
    config.CONFIG_FILE.write_text("pipeline:\n  workspace:\n    type: git\n    root: /tmp/old\n")
    _clear_pipeline_cache()
    assert config.load_review_pipeline_config().workspace_manager.options == {"root": "/tmp/old"}
    config.CONFIG_FILE.write_text("pipeline:\n  workspace:\n    type: git\n    root: /tmp/new\n")
    config.load_pipeline_config.cache_clear()
    assert config.load_review_pipeline_config().workspace_manager.options == {"root": "/tmp/new"}


def test_pipeline_config_rejects_unknown_provider(allowlist):
    config.CONFIG_FILE.write_text("pipeline:\n  executor:\n    type: unknown\n")
    _clear_pipeline_cache()
    try:
        config.load_pipeline_config()
    except ValueError as exc:
        assert "unknown provider type: unknown" in str(exc)
    else:
        raise AssertionError("expected unknown provider error")
