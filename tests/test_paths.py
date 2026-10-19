"""Tests for checkout-independent configuration and data path resolution."""

from __future__ import annotations

from pathlib import Path

from orchestrator.infra.filesystem import paths


def test_xdg_defaults_ignore_the_working_directory():
    env: dict[str, str] = {}

    assert paths.config_home(env) == Path.home() / ".config" / "ai-orchestrator"
    assert paths.data_home(env) == Path.home() / ".local" / "share" / "ai-orchestrator"
    assert paths.env_file(env) == Path.home() / ".config" / "ai-orchestrator" / ".env"
    assert paths.config_file(env) == Path.home() / ".config" / "ai-orchestrator" / "config.yaml"
    assert paths.private_key_file(env) == Path.home() / ".config" / "ai-orchestrator" / "key.pem"
    assert paths.data_dir(env) == Path.home() / ".local" / "share" / "ai-orchestrator"
    assert paths.state_dir(env) == Path.home() / ".local" / "share" / "ai-orchestrator" / "state"
    assert paths.logs_dir(env) == Path.home() / ".local" / "share" / "ai-orchestrator" / "logs"
    assert paths.repos_dir(env) == Path.home() / "agent-repos"
    assert paths.workspaces_dir(env) == Path.home() / "agent-workspaces"


def test_absolute_xdg_home_overrides_are_used():
    env = {
        "XDG_CONFIG_HOME": "/custom/config",
        "XDG_DATA_HOME": "/custom/data",
    }

    assert paths.config_home(env) == Path("/custom/config/ai-orchestrator")
    assert paths.config_file(env) == Path("/custom/config/ai-orchestrator/config.yaml")
    assert paths.data_home(env) == Path("/custom/data/ai-orchestrator")
    assert paths.state_dir(env) == Path("/custom/data/ai-orchestrator/state")
    assert paths.logs_dir(env) == Path("/custom/data/ai-orchestrator/logs")


def test_relative_xdg_home_values_are_ignored():
    env = {
        "XDG_CONFIG_HOME": "relative/config",
        "XDG_DATA_HOME": "relative/data",
    }

    assert paths.config_home(env) == Path.home() / ".config" / "ai-orchestrator"
    assert paths.data_home(env) == Path.home() / ".local" / "share" / "ai-orchestrator"


def test_orchestrator_overrides_take_precedence_over_xdg_defaults():
    env = {
        "XDG_CONFIG_HOME": "/custom/config",
        "XDG_DATA_HOME": "/custom/data",
        "ORCHESTRATOR_ENV_FILE": "/override/.env",
        "ORCHESTRATOR_CONFIG_FILE": "/override/config.yaml",
        "ORCHESTRATOR_DATA_DIR": "/override/data",
        "ORCHESTRATOR_REPOS_DIR": "/override/repos",
        "ORCHESTRATOR_WORKSPACES_DIR": "/override/workspaces",
        "ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE": "/override/key.pem",
    }

    assert paths.env_file(env) == Path("/override/.env")
    assert paths.config_file(env) == Path("/override/config.yaml")
    assert paths.data_dir(env) == Path("/override/data")
    assert paths.state_dir(env) == Path("/override/data/state")
    assert paths.logs_dir(env) == Path("/override/data/logs")
    assert paths.repos_dir(env) == Path("/override/repos")
    assert paths.workspaces_dir(env) == Path("/override/workspaces")
    assert paths.private_key_file(env) == Path("/override/key.pem")


def test_tilde_overrides_are_expanded():
    env = {
        "ORCHESTRATOR_DATA_DIR": "~/custom-data",
        "ORCHESTRATOR_REPOS_DIR": "~/custom-repos",
        "ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE": "~/keys/key.pem",
    }

    assert paths.data_dir(env) == Path.home() / "custom-data"
    assert paths.state_dir(env) == Path.home() / "custom-data" / "state"
    assert paths.repos_dir(env) == Path.home() / "custom-repos"
    assert paths.private_key_file(env) == Path.home() / "keys" / "key.pem"


def test_plain_mapping_is_accepted_instead_of_os_environ():
    # A plain dict proves resolution does not depend on the process environment.
    env = {"ORCHESTRATOR_DATA_DIR": "/mapping/data"}

    assert paths.data_dir(env) == Path("/mapping/data")
    assert paths.data_dir() != Path("/mapping/data")
