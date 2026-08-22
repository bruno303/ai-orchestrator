"""Tests for the Claude Code CLI provider using a fake binary."""

from __future__ import annotations

import json
import threading

import pytest

from orchestrator.application.ports import ExecutionRequest, ReviewRequest
from orchestrator.domain import Context
from orchestrator.infra.claude import executor as claude
from orchestrator.main import config


def test_run_claude_success(tmp_path, clean_env):
    result = claude.run_claude(tmp_path, "plan", "planning the implementation of issue")

    assert result.exit_code == 0
    assert "Plan written" in result.stdout


def test_run_claude_passes_workspace_model_and_permission_options_without_effort_flag(
    tmp_path, clean_env, monkeypatch
):
    args_file = tmp_path / "args.txt"
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_CLAUDE_MODEL_FILE", str(model_file))

    result = claude.run_claude(
        tmp_path,
        "build",
        "implementing GitHub issue #1",
        model="sonnet",
        variant="high",
        permission_mode="acceptEdits",
    )

    assert result.exit_code == 0
    assert f"dir={tmp_path} output=text permission=acceptEdits agent=" in args_file.read_text()
    assert "model=sonnet env_effort=high" in model_file.read_text()


def test_claude_executor_uses_request_log_file_and_provider_options(tmp_path, clean_env):
    log_file = tmp_path / "execution.log"
    result = claude.ClaudeExecutor({"model": "opus", "variant": "medium"}).execute(
        ExecutionRequest(
            "task-1",
            str(tmp_path),
            "planning the implementation of issue",
            "plan",
            model="sonnet",
            variant="low",
            log_file=str(log_file),
        )
    )

    assert result.success
    assert result.exit_code == 0
    assert "Plan written" in result.stdout
    content = log_file.read_text()
    assert "[orchestrator] claude -p --output-format text --model opus" in content
    assert "variant=medium" in content
    assert "agent=plan" in content
    assert "--effort" not in content


def test_run_claude_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")

    result = claude.run_claude(tmp_path, "plan", "planning the implementation of issue")

    assert result.exit_code == 1
    assert "simulated failure" in result.stdout


def test_run_claude_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "5")

    with pytest.raises(claude.ClaudeError, match="timed out"):
        claude.run_claude(tmp_path, "plan", "planning the implementation of issue", timeout=1)


def test_run_claude_missing_workspace(tmp_path):
    with pytest.raises(claude.ClaudeError, match="does not exist"):
        claude.run_claude(tmp_path / "missing", "plan", "planning the implementation of issue")


def test_run_claude_streams_to_log_file(tmp_path, clean_env, monkeypatch):
    log_file = tmp_path / "plan.log"
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")
    result_holder: dict[str, claude.ClaudeResult] = {}

    def run() -> None:
        result_holder["result"] = claude.run_claude(
            tmp_path,
            "plan",
            "planning the implementation of issue",
            log_file=log_file,
        )

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=4)

    assert log_file.exists()
    assert "Plan written" in log_file.read_text()
    assert result_holder["result"].exit_code == 0


def test_claude_review_executor_uses_plan_mode_and_review_model(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_CLAUDE_MODEL_FILE", str(model_file))

    result = claude.ClaudeReviewExecutor(
        {"model_config": config.ModelConfig("opus", "medium")}
    ).execute(
        ReviewRequest(
            "review:r#1",
            "r",
            str(tmp_path),
            "Use only the code-review skill and return ONLY valid JSON.",
            Context(),
        )
    )

    assert result.success
    assert result.verdict == "comment"
    assert "permission=plan" in args_file.read_text()
    assert "model=opus env_effort=medium" in model_file.read_text()


def test_claude_review_executor_rejects_invalid_structured_result(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.infra.claude.executor.run_claude",
        lambda *args, **kwargs: claude.ClaudeResult(
            0,
            json.dumps({
                "verdict": "comment",
                "summary": "x",
                "findings": [{"message": "x", "side": "middle"}],
                "checks": [],
            }),
            "",
            0.1,
        ),
    )

    result = claude.ClaudeReviewExecutor().execute(
        ReviewRequest("review:r#1", "r", "/tmp", "review", Context())
    )

    assert not result.success
    assert "invalid structured" in result.summary
