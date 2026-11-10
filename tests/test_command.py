"""Unit tests for the workspace command step backend."""

from __future__ import annotations

from orchestrator.infra.filesystem.command import run_workspace_command


def test_run_workspace_command_captures_success_and_cwd(tmp_path):
    result = run_workspace_command(str(tmp_path), "pwd", 30)

    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path)
    assert result.stderr == ""
    assert result.duration_seconds > 0


def test_run_workspace_command_reports_nonzero_failure(tmp_path):
    result = run_workspace_command(str(tmp_path), "exit 3", 30)

    assert result.success is False
    assert result.exit_code == 3


def test_run_workspace_command_survives_timeout(tmp_path):
    result = run_workspace_command(str(tmp_path), "sleep 5", 1)

    assert result.success is False
    assert result.exit_code == 124
    assert result.stdout == ""
    assert "timed out" in result.stderr
    assert "timed out after 1s" in result.stderr


def test_run_workspace_command_survives_missing_workspace(tmp_path):
    result = run_workspace_command(str(tmp_path / "missing"), "echo hi", 30)

    # The OSError path never raises and reports an unsuccessful result.
    assert result.success is False
    assert result.exit_code == 127
    assert result.stdout == ""
    assert result.stderr != ""


def test_run_workspace_command_writes_captured_output_to_log_file(tmp_path):
    log_file = tmp_path / "command.log"

    result = run_workspace_command(str(tmp_path), "echo out; echo err >&2", 30, str(log_file))

    assert result.success is True
    assert log_file.read_text() == "out\nerr\n"


def test_run_workspace_command_creates_missing_log_parent_directory(tmp_path):
    log_file = tmp_path / "missing" / "command.log"

    result = run_workspace_command(str(tmp_path), "echo ok", 30, str(log_file))

    assert result.success is True
    assert log_file.read_text().strip() == "ok"


def test_run_workspace_command_tolerates_log_write_failures(tmp_path):
    # A file where the log's parent directory belongs blocks every IO step;
    # the failure is swallowed and the command result is unaffected.
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked")

    result = run_workspace_command(str(tmp_path), "echo ok", 30, str(blocked_parent / "command.log"))

    assert result.success is True


def test_run_workspace_command_without_log_file_writes_nothing(tmp_path):
    result = run_workspace_command(str(tmp_path), "echo ok", 30)

    assert result.success is True
    assert list(tmp_path.iterdir()) == []
