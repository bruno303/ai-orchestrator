"""Checkout-independent resolution of configuration, data, and workspace paths.

Every path is resolved from an explicit override or the XDG base-directory
specification.  No path is derived from ``__file__`` or the current working
directory, so installed code behaves the same wherever it runs from.

``os.environ`` is consulted at call time, which means a ``.env`` file loaded
after this module is imported is still honoured by later resolutions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

APP_DIR_NAME = "ai-orchestrator"


def _value(name: str, env: Mapping[str, str]) -> str | None:
    value = env.get(name)
    return value if value else None


def _xdg_home(variable: str, fallback: Path, env: Mapping[str, str]) -> Path:
    value = _value(variable, env)
    if value is not None and Path(value).is_absolute():
        return (Path(value) / APP_DIR_NAME).expanduser()
    return (fallback / APP_DIR_NAME).expanduser()


def _override(name: str, fallback: Path, env: Mapping[str, str]) -> Path:
    value = _value(name, env)
    return Path(value if value is not None else fallback).expanduser()


def config_home(env: Mapping[str, str] = os.environ) -> Path:
    """Return the application configuration directory."""
    return _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config", env)


def data_home(env: Mapping[str, str] = os.environ) -> Path:
    """Return the application data directory."""
    return _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share", env)


def env_file(env: Mapping[str, str] = os.environ) -> Path:
    """Return the dotenv file used for deployment overrides."""
    return _override("ORCHESTRATOR_ENV_FILE", config_home(env) / ".env", env)


def config_file(env: Mapping[str, str] = os.environ) -> Path:
    """Return the main YAML configuration file."""
    return _override("ORCHESTRATOR_CONFIG_FILE", config_home(env) / "config.yaml", env)


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Return the root directory for runtime data."""
    return _override("ORCHESTRATOR_DATA_DIR", data_home(env), env)


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Return the directory holding durable runtime state."""
    return (data_dir(env) / "state").expanduser()


def logs_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Return the directory holding per-task logs."""
    return (data_dir(env) / "logs").expanduser()


def repos_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Return the directory holding cached repository clones."""
    return _override("ORCHESTRATOR_REPOS_DIR", Path.home() / "agent-repos", env)


def workspaces_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Return the directory holding per-task workspaces."""
    return _override("ORCHESTRATOR_WORKSPACES_DIR", Path.home() / "agent-workspaces", env)


def private_key_file(env: Mapping[str, str] = os.environ) -> Path:
    """Return the GitHub App private key file."""
    return _override(
        "ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE",
        config_home(env) / "key.pem",
        env,
    )
