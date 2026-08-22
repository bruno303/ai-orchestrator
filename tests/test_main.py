"""CLI stateless behavior."""

from types import SimpleNamespace

import pytest

from orchestrator.infra.github import client as github
from orchestrator.main import cli
from orchestrator.main.cli import _poll_reviews, main


def test_removed_persistence_commands_are_absent(capsys):
    with pytest.raises(SystemExit): main(["list"])
    assert "invalid choice" in capsys.readouterr().err


def test_run_skips_developed_issue_without_force(allowlist, monkeypatch):
    monkeypatch.setattr(github, "get_issue", lambda *args: github.Issue(1, "t", "b", "u", ["ai-developed"]))
    with pytest.raises(SystemExit, match="already labeled ai-developed"):
        main(["run", "company/backend#1"])


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


def test_keyboard_interrupt_prints_generic_stop_message(monkeypatch, capsys):
    def interrupt(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_review", interrupt)

    with pytest.raises(SystemExit) as exc_info:
        main(["review", "--once"])

    assert exc_info.value.code == 130
    assert capsys.readouterr().out == "\nprocess stopped.\n"
