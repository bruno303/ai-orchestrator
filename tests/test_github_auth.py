"""Tests for GitHub App token and Git identity configuration."""

from __future__ import annotations

import json

from orchestrator.infra.github import auth as github_auth


def test_gh_environment_uses_installation_token(monkeypatch):
    monkeypatch.setattr(github_auth, "installation_token", lambda: "installation-token")

    environment = github_auth.gh_environment()

    assert environment["GH_TOKEN"] == "installation-token"


def test_git_environment_uses_bot_credentials_and_identity(monkeypatch):
    monkeypatch.setattr(github_auth, "installation_token", lambda: "installation-token")

    environment = github_auth.git_environment()

    assert environment["GIT_AUTHOR_NAME"] == "bruno303-ai-agent-bot[bot]"
    assert environment["GIT_COMMITTER_NAME"] == "bruno303-ai-agent-bot[bot]"
    assert environment["GIT_AUTHOR_EMAIL"] == "123+bruno303-ai-agent-bot[bot]@users.noreply.github.com"
    assert environment["GIT_COMMITTER_EMAIL"] == environment["GIT_AUTHOR_EMAIL"]
    assert "installation-token" not in json.dumps(environment)


def test_user_identity_preserves_host_environment_without_bot_overrides(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "user-token")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Local User")
    monkeypatch.setattr(github_auth, "installation_token", lambda: (_ for _ in ()).throw(AssertionError()))

    identity = github_auth.identity_from_options({"auth": "user"})

    assert identity.gh_environment()["GH_TOKEN"] == "user-token"
    assert identity.git_environment()["GIT_AUTHOR_NAME"] == "Local User"


def test_identity_defaults_to_bot_and_rejects_unknown_modes():
    assert github_auth.identity_from_options({}).mode == "bot"
    import pytest
    with pytest.raises(ValueError, match="'bot' or 'user'"):
        github_auth.identity_from_options({"auth": "token"})
