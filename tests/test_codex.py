"""Tests for the Codex CLI provider using a fake binary."""

from __future__ import annotations

import json
import threading

import pytest

from orchestrator.application.ports import ExecutionRequest, ReviewRequest
from orchestrator.domain import Context
from orchestrator.infra.codex import executor as codex
from orchestrator.main import config


def test_run_codex_success(tmp_path, clean_env):
    result = codex.run_codex(tmp_path, "plan", "planning the implementation of issue")

    assert result.exit_code == 0
    assert "Plan written" in result.stdout


def test_run_codex_passes_workspace_sandbox_and_model_options(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_CODEX_MODEL_FILE", str(model_file))

    codex.run_codex(
        tmp_path,
        "build",
        "implementing GitHub issue #1",
        model="openai/gpt-5.6-luna",
        variant="high",
        sandbox="workspace-write",
    )

    assert f"dir={tmp_path} sandbox=workspace-write approval=never" in args_file.read_text()
    assert "model=openai/gpt-5.6-luna reasoning=high" in model_file.read_text()


def test_codex_executor_uses_request_log_file(tmp_path, clean_env):
    log_file = tmp_path / "execution.log"
    result = codex.CodexExecutor().execute(
        ExecutionRequest(
            "task-1",
            str(tmp_path),
            "planning the implementation of issue",
            "plan",
            log_file=str(log_file),
        )
    )

    assert result.success
    assert result.exit_code == 0
    assert "Plan written" in result.stdout
    assert "[orchestrator] codex exec" in log_file.read_text()


def test_run_codex_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")

    result = codex.run_codex(tmp_path, "plan", "planning the implementation of issue")

    assert result.exit_code == 1
    assert "simulated failure" in result.stdout


def test_run_codex_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "5")

    with pytest.raises(codex.CodexError, match="timed out"):
        codex.run_codex(tmp_path, "plan", "planning the implementation of issue", timeout=1)


def test_run_codex_missing_workspace(tmp_path):
    with pytest.raises(codex.CodexError, match="does not exist"):
        codex.run_codex(tmp_path / "missing", "plan", "planning the implementation of issue")


def test_run_codex_streams_to_log_file(tmp_path, clean_env, monkeypatch):
    log_file = tmp_path / "plan.log"
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")
    result_holder: dict[str, codex.CodexResult] = {}

    def run() -> None:
        result_holder["result"] = codex.run_codex(
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


def test_codex_review_executor_uses_read_only_sandbox_and_review_model(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_CODEX_MODEL_FILE", str(model_file))

    result = codex.CodexReviewExecutor(
        {"model_config": config.ModelConfig("provider/review", "medium")}
    ).execute(
        ReviewRequest(
            "review:r#1",
            "r",
            str(tmp_path),
            "Use only the `/code-review` skill and return ONLY valid JSON.",
            Context(),
        )
    )

    assert result.success
    assert result.verdict == "comment"
    assert "sandbox=read-only" in args_file.read_text()
    assert "model=provider/review reasoning=medium" in model_file.read_text()


def test_codex_review_executor_rejects_invalid_structured_result(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.infra.codex.executor.run_codex",
        lambda *args, **kwargs: codex.CodexResult(
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

    result = codex.CodexReviewExecutor().execute(
        ReviewRequest("review:r#1", "r", "/tmp", "review", Context())
    )

    assert not result.success
    assert "invalid structured" in result.summary
