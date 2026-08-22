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


def test_run_uses_the_configured_input_client_for_issue_metadata(allowlist, monkeypatch):
    calls = []

    class Client:
        GitHubError = github.GitHubError

        def get_issue(self, repository, number):
            calls.append(("issue", repository, number))
            return github.Issue(number, "title", "body", "url", [])

        def get_repository(self, repository):
            calls.append(("repository", repository))
            return {"clone_url": "https://github.com/company/backend.git", "default_branch": "main"}

        @staticmethod
        def https_clone_url(metadata, repository):
            return metadata["clone_url"]

    runtime = SimpleNamespace(
        input_source=SimpleNamespace(github_client=Client()), executor=object(),
        workspace_manager=object(), destination=object(), execution_runtime=object(),
    )
    monkeypatch.setattr(cli, "compose_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "_run_graph", lambda seed, task_id, **kwargs: {"task_id": task_id, "status": "completed"})

    cli.cmd_run(SimpleNamespace(issue_ref="company/backend#8", force=False))

    assert calls == [("issue", "company/backend", 8), ("issue", "company/backend", 8), ("repository", "company/backend")]


def test_reset_uses_the_configured_destination_client(allowlist, monkeypatch):
    calls = []
    destination = SimpleNamespace(github_client=SimpleNamespace(
        remove_issue_label=lambda repository, number, label: calls.append((repository, number, label))
    ))
    monkeypatch.setattr(cli, "compose_execution_runtime", lambda: SimpleNamespace(destination=destination))

    cli.cmd_reset(SimpleNamespace(issue_ref="company/backend#8"))

    assert calls == [("company/backend", 8, "ai-developed")]


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
