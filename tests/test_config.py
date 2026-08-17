"""Tests for model configuration parsing and env overrides."""

from __future__ import annotations

from orchestrator import config


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
