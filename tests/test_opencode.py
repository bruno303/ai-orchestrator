"""Tests for the opencode wrapper using a fake opencode binary."""

from __future__ import annotations

import pytest

from orchestrator.infra.opencode import executor as opencode
from orchestrator.application.ports import (
    DiscussionRequest,
    ExecutionRequest,
    ExecutionResult,
    ReviewRequest,
    TriageRequest,
)
from orchestrator.domain import Context


def test_run_opencode_success(tmp_path, clean_env):
    result = opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue")
    assert result.exit_code == 0
    assert "Plan written" in result.stdout
    assert result.stderr == "diagnostic from stderr\n"


def test_opencode_executor_adapts_run_result(tmp_path, clean_env):
    result = opencode.OpenCodeExecutor().execute(
        ExecutionRequest("task-1", str(tmp_path), "planning the implementation of issue", "plan")
    )
    assert result == ExecutionResult(
        True, 0, stdout=result.stdout, stderr=result.stderr, duration_seconds=result.duration_seconds
    )


def test_run_opencode_passes_flags(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_OPCODE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_OPCODE_EXPECTED_CWD", str(tmp_path))
    opencode.run_opencode(tmp_path, "build", "implementing GitHub issue #1")
    line = args_file.read_text()
    assert "agent=build" in line
    assert f"dir={tmp_path}" in line


def test_run_opencode_ignores_host_binary_override(tmp_path, monkeypatch):
    captured = {}

    class Runner:
        def run(self, command, workspace, **options):
            captured["command"] = command
            from orchestrator.infra.sandbox.runner import SandboxResult
            return SandboxResult(0, "", "", 0)

    monkeypatch.setenv("ORCHESTRATOR_OPENCODE_BIN", "/host/custom-opencode")
    opencode.run_opencode(tmp_path, "plan", "prompt", runner=Runner())

    assert captured["command"][0] == "opencode"


def test_run_opencode_uses_default_agent_when_not_provided(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_OPCODE_ARGS_FILE", str(args_file))
    opencode.run_opencode(tmp_path, None, "planning the implementation of issue")
    assert f"agent= dir={tmp_path}" in args_file.read_text()


def test_run_opencode_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_FAIL", "1")
    result = opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue")
    assert result.exit_code == 1
    assert "simulated stdout failure" in result.stdout
    assert "simulated stderr failure" in result.stderr


def test_opencode_discussion_failure_does_not_publish_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_FAIL", "1")

    result = opencode.OpenCodeDiscussionExecutor().execute(
        DiscussionRequest("task-1", "owner/repo", str(tmp_path), "answer the question")
    )

    assert not result.success
    assert result.response == ""
    assert "simulated stdout failure" not in result.response
    assert "simulated stdout failure" in result.stderr
    assert "simulated stderr failure" in result.stderr


def test_opencode_review_failure_preserves_both_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_FAIL", "1")

    result = opencode.OpenCodeReviewExecutor().execute(
        ReviewRequest("review:r#1", "owner/repo", str(tmp_path), "review", Context())
    )

    assert not result.success
    assert "simulated stdout failure" in result.summary
    assert "simulated stderr failure" in result.summary


def test_opencode_triage_failure_preserves_both_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_FAIL", "1")

    result = opencode.OpenCodeTriageExecutor().execute(
        TriageRequest("triage-1", "owner/repo", str(tmp_path), "triage", context=Context())
    )

    assert not result.success
    assert "simulated stdout failure" in result.summary
    assert "simulated stderr failure" in result.summary


def test_run_opencode_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_SLEEP", "5")
    with pytest.raises(opencode.OpenCodeError, match="timed out"):
        opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue", timeout=1)


def test_run_opencode_missing_workspace(tmp_path):
    with pytest.raises(opencode.OpenCodeError, match="does not exist"):
        opencode.run_opencode(tmp_path / "nope", "plan", "planning the implementation of issue")


def test_run_opencode_streams_to_log_file(tmp_path, clean_env, monkeypatch):
    """Output must be written to the log file live, before the process exits."""
    from pathlib import Path

    log_file = tmp_path / "plan.log"
    # FAKE_OPCODE_SLEEP keeps the process alive while we verify the log is being written.
    monkeypatch.setenv("FAKE_OPCODE_SLEEP", "2")

    import threading

    result_holder: dict = {}

    def run():
        result_holder["result"] = opencode.run_opencode(
            tmp_path, "plan", "planning the implementation of issue", log_file=log_file
        )

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=4)
    assert log_file.exists()
    content = log_file.read_text()
    assert "Plan written" in content
    assert "diagnostic from stderr" in content
    assert result_holder["result"].exit_code == 0


def test_run_opencode_no_false_positive(tmp_path, clean_env):
    result = opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue")
    assert result.exit_code == 0
    assert "Plan written" in result.stdout


def test_run_opencode_passes_model_flags(tmp_path, clean_env, monkeypatch):
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_OPCODE_MODEL_FILE", str(model_file))
    opencode.run_opencode(
        tmp_path,
        "plan",
        "planning the implementation of issue",
        model="verboo/glm-4.7-flash",
        variant="high",
    )
    line = model_file.read_text()
    assert "model=verboo/glm-4.7-flash variant=high reference=verboo/glm-4.7-flash#high" in line


def test_run_opencode_passes_unqualified_model_without_variant(tmp_path, clean_env, monkeypatch):
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_OPCODE_MODEL_FILE", str(model_file))
    opencode.run_opencode(tmp_path, None, "planning the implementation of issue", model="provider/model")
    assert "model=provider/model variant= reference=provider/model" in model_file.read_text()


def test_run_opencode_does_not_append_second_variant(tmp_path, clean_env, monkeypatch):
    model_file = tmp_path / "models.txt"
    monkeypatch.setenv("FAKE_OPCODE_MODEL_FILE", str(model_file))
    opencode.run_opencode(tmp_path, None, "planning the implementation of issue", model="provider/model#existing", variant="new")
    assert "model=provider/model variant=existing reference=provider/model#existing" in model_file.read_text()


def test_run_opencode_logs_model_header(tmp_path, clean_env, monkeypatch):
    log_file = tmp_path / "plan.log"
    opencode.run_opencode(
        tmp_path,
        "plan",
        "planning the implementation of issue",
        log_file=log_file,
        model="verboo/deepseek-v4-flash",
        variant="high",
    )
    content = log_file.read_text()
    assert content.startswith("[orchestrator] opencode run --agent plan --model verboo/deepseek-v4-flash#high")
