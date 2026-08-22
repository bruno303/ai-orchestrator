"""CLI stateless behavior."""

import pytest

from orchestrator.infra.github import client as github
from orchestrator.main.cli import main


def test_removed_persistence_commands_are_absent(capsys):
    with pytest.raises(SystemExit): main(["list"])
    assert "invalid choice" in capsys.readouterr().err


def test_run_skips_developed_issue_without_force(allowlist, monkeypatch):
    monkeypatch.setattr(github, "get_issue", lambda *args: github.Issue(1, "t", "b", "u", ["ai-developed"]))
    with pytest.raises(SystemExit, match="already labeled ai-developed"):
        main(["run", "company/backend#1"])
