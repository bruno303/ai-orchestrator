"""Tests for the opencode wrapper using a fake opencode binary."""

from __future__ import annotations

import pytest

from orchestrator.infra.opencode import executor as opencode
from orchestrator.application.ports import ExecutionRequest, ExecutionResult


def test_run_opencode_success(tmp_path, clean_env):
    result = opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue")
    assert result.exit_code == 0
    assert "Plan written" in result.stdout


def test_opencode_executor_adapts_run_result(tmp_path, clean_env):
    result = opencode.OpenCodeExecutor().execute(
        ExecutionRequest("task-1", str(tmp_path), "planning the implementation of issue", "plan")
    )
    assert result == ExecutionResult(True, 0, stdout=result.stdout, duration_seconds=result.duration_seconds)


def test_run_opencode_passes_flags(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_OPCODE_ARGS_FILE", str(args_file))
    opencode.run_opencode(tmp_path, "build", "implementing GitHub issue #1")
    line = args_file.read_text()
    assert "agent=build" in line
    assert f"dir={tmp_path}" in line


def test_run_opencode_uses_default_agent_when_not_provided(tmp_path, clean_env, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_OPCODE_ARGS_FILE", str(args_file))
    opencode.run_opencode(tmp_path, None, "planning the implementation of issue")
    assert "agent= dir=" in args_file.read_text()


def test_run_opencode_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_FAIL", "1")
    result = opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue")
    assert result.exit_code == 1
    assert "simulated failure" in result.stdout  # stderr merged into stdout


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
    assert result_holder["result"].exit_code == 0


def test_run_opencode_detects_repeat_loop(tmp_path, clean_env, monkeypatch):
    """Degenerate repeated output must raise DegenerateOutputError."""
    monkeypatch.setenv("FAKE_OPCODE_LOOP", "1")
    with pytest.raises(opencode.DegenerateOutputError):
        opencode.run_opencode(tmp_path, "plan", "planning the implementation of issue")


def test_run_opencode_skips_loop_detection_when_disabled(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("FAKE_OPCODE_LOOP", "1")
    result = opencode.run_opencode(
        tmp_path,
        "plan",
        "planning the implementation of issue",
        detect_degenerate=False,
    )
    assert result.exit_code == 0


def test_run_opencode_no_false_positive(tmp_path, clean_env):
    """Normal output must not trip the loop detector."""
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
    assert "model=verboo/glm-4.7-flash variant=high" in line


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
    assert content.startswith("[orchestrator] opencode run --agent plan --model verboo/deepseek-v4-flash --variant high")


def test_detect_loop_unit():
    loop_line = "PERMIT ME NOW to emit exactly one invocation card"
    identical = [loop_line] * 100
    assert opencode.detect_loop(identical, 100, 20, 0.1)

    healthy = [f"progress line {i}" for i in range(100)]
    assert not opencode.detect_loop(healthy, 100, 20, 0.1)

    # 50 lines alternating between 3 unique values: distinct/window = 3/50 = 0.06 <= 0.1.
    alternating = [f"step {i % 3}" for i in range(50)]
    assert opencode.detect_loop(alternating, 100, 20, 0.1)

    assert not opencode.detect_loop([], 100, 20, 0.1)
