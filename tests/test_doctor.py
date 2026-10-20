"""Tests for ``ai-orchestrator doctor`` host diagnostics."""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.infra.github import auth as github_auth
from orchestrator.main import cli, config, doctor


def _configure(tmp_path, monkeypatch, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    config.load_pipeline_config.cache_clear()
    config.load_repository_config.cache_clear()
    return path


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {check.name: check for check in checks}


def test_python_check_is_ok_on_a_supported_interpreter():
    check = _by_name(doctor.run_checks())["python"]

    assert check.status == "ok"
    assert check.detail


def test_missing_executor_binary_is_warn_not_fail(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories:\n  - name: company/backend\n")
    for variable in doctor.EXECUTOR_ENV_VARS.values():
        monkeypatch.setenv(variable, "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(doctor.shutil, "which", lambda binary: None)

    checks = _by_name(doctor.run_checks())

    for stage in ("execution", "review", "triage"):
        check = checks[f"executor ({stage})"]
        assert check.status == "warn"
        assert "not found on PATH" in check.detail


def test_executor_resolves_a_binary_from_the_adapter_fallback_directory(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories:\n  - name: company/backend\n")
    home = tmp_path / "home"
    binary = home / ".opencode" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ORCHESTRATOR_OPENCODE_BIN", raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    calls: list[list[str]] = []

    def fake_run(command, *, timeout=doctor.TIMEOUT_SECONDS):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="opencode 1.0", stderr="")

    monkeypatch.setattr(doctor, "_run", fake_run)

    check = _by_name(doctor.run_checks())["executor (execution)"]

    assert check.status == "ok"
    assert check.detail == "opencode 1.0"
    assert [str(binary), "--version"] in calls


def test_missing_private_key_fails_and_names_the_path(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories:\n  - name: company/backend\n")
    key_path = tmp_path / "missing-key.pem"
    monkeypatch.setattr(github_auth, "PRIVATE_KEY_FILE", key_path)

    check = _by_name(doctor.run_checks())["github app key"]

    assert check.status == "fail"
    assert str(key_path) in check.detail


def test_private_key_is_skipped_when_no_stage_uses_bot_auth(tmp_path, monkeypatch):
    providers = (
        "    input_source: {type: github_polling, auth: user}\n"
        "    executor: {type: opencode}\n"
        "    workspace_manager: {type: git, auth: user}\n"
        "    destination: {type: github, auth: user}\n"
    )
    triage = (
        "    input_source: {type: github_polling, auth: user}\n"
        "    executor: {type: opencode}\n"
        "    destination: {type: github, auth: user}\n"
    )
    _configure(
        tmp_path,
        monkeypatch,
        "repositories:\n"
        "  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        f"{providers}"
        "  review:\n"
        f"{providers}"
        "  triage:\n"
        f"{triage}",
    )

    check = _by_name(doctor.run_checks())["github app key"]

    assert check.status == "ok"
    assert "skipped" in check.detail


def test_executor_auth_is_never_inferred_as_bot_auth(tmp_path, monkeypatch):
    """Regression: executors do not consume ``auth``, so an omitted executor
    auth must not turn an all-user configuration into a bot-auth one."""
    providers = (
        "    input_source: {type: github_polling, auth: user}\n"
        "    executor: {type: opencode}\n"
        "    workspace_manager: {type: git, auth: user}\n"
        "    destination: {type: github, auth: user}\n"
    )
    triage = (
        "    input_source: {type: github_polling, auth: user}\n"
        "    executor: {type: opencode}\n"
        "    destination: {type: github, auth: user}\n"
    )
    _configure(
        tmp_path,
        monkeypatch,
        "repositories:\n"
        "  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        f"{providers}"
        "  review:\n"
        f"{providers}"
        "  triage:\n"
        f"{triage}",
    )
    # A missing key would fail the check if the executor counted as bot auth.
    monkeypatch.setattr(github_auth, "PRIVATE_KEY_FILE", tmp_path / "absent-key.pem")

    checks = _by_name(doctor.run_checks())

    assert checks["github app key"].status == "ok"
    assert "skipped" in checks["github app key"].detail


def test_empty_allowlist_fails_and_points_at_init(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories: []\n")

    check = _by_name(doctor.run_checks())["repository allowlist"]

    assert check.status == "fail"
    assert "ai-orchestrator init" in check.detail


def test_missing_config_file_fails_config_and_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "absent.yaml")
    config.load_pipeline_config.cache_clear()
    config.load_repository_config.cache_clear()

    checks = _by_name(doctor.run_checks())

    assert checks["config file"].status == "fail"
    assert checks["repository allowlist"].status == "fail"
    assert "ai-orchestrator init" in checks["repository allowlist"].detail


def test_unparseable_config_file_fails_with_the_parser_message(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories: [unclosed\n")

    check = _by_name(doctor.run_checks())["config file"]

    assert check.status == "fail"
    assert check.detail
    assert "\n" not in check.detail
    # Collapsing whitespace keeps the parser message and its location intact.
    assert "while parsing a flow sequence" in check.detail
    assert "line 1, column 15" in check.detail


def test_cli_prints_one_line_per_check_for_an_unparseable_config(tmp_path, monkeypatch, capsys):
    _configure(tmp_path, monkeypatch, "repositories: [unclosed\n")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["doctor"])

    assert excinfo.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(doctor.run_checks())
    for line in lines:
        assert "\n" not in line
        assert line.startswith(("[ok] ", "[warn] ", "[fail] "))
    config_line = next(line for line in lines if line.startswith("[fail] config file: "))
    assert "while parsing a flow sequence" in config_line


def test_data_directories_are_created_and_reported(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "LOGS_DIR", data_dir / "logs")
    monkeypatch.setattr(config, "STATE_DIR", data_dir / "state")

    check = _by_name(doctor.run_checks())["data directories"]

    assert check.status == "ok"
    assert (data_dir / "state").is_dir()
    assert (data_dir / "logs").is_dir()


def test_gh_auth_is_skipped_when_every_stage_uses_bot_auth(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories:\n  - name: company/backend\n")
    key_path = tmp_path / "key.pem"
    key_path.write_text("key")
    monkeypatch.setattr(github_auth, "PRIVATE_KEY_FILE", key_path)

    check = _by_name(doctor.run_checks())["gh auth"]

    assert check.status == "ok"
    assert "skipped" in check.detail


def test_gh_auth_status_runs_when_a_stage_uses_user_auth(tmp_path, monkeypatch):
    _configure(
        tmp_path,
        monkeypatch,
        "repositories:\n"
        "  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    input_source: {type: github_polling, auth: user}\n",
    )
    calls: list[list[str]] = []

    def fake_run(command, *, timeout=doctor.TIMEOUT_SECONDS):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Logged in", stderr="")

    monkeypatch.setattr(doctor, "_run", fake_run)

    check = _by_name(doctor.run_checks())["gh auth"]

    assert check.status == "ok"
    assert any(command[1:3] == ["auth", "status"] for command in calls)


def test_run_checks_never_raises_when_subprocess_run_raises(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories:\n  - name: company/backend\n")

    def boom(*args, **kwargs):
        raise OSError("subprocess unavailable")

    monkeypatch.setattr(doctor.subprocess, "run", boom)

    checks = doctor.run_checks()

    assert checks
    assert {check.status for check in checks} <= {"ok", "warn", "fail"}
    git_check = _by_name(checks)["git"]
    assert git_check.status in {"warn", "fail"}


def test_run_checks_converts_timeouts_into_warn_lines(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, "repositories:\n  - name: company/backend\n")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=doctor.TIMEOUT_SECONDS)

    monkeypatch.setattr(doctor.subprocess, "run", timeout)

    checks = _by_name(doctor.run_checks())

    assert checks["git"].status == "warn"
    assert "timed out" in checks["git"].detail


def test_cli_prints_one_line_per_check_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.doctor,
        "run_checks",
        lambda: [
            doctor.Check("python", "ok", "3.11.15"),
            doctor.Check("gh", "warn", "gh --version failed"),
        ],
    )

    cli.main(["doctor"])

    assert capsys.readouterr().out.splitlines() == [
        "[ok] python: 3.11.15",
        "[warn] gh: gh --version failed",
    ]


def test_cli_exits_one_when_a_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.doctor,
        "run_checks",
        lambda: [
            doctor.Check("python", "ok", "3.11.15"),
            doctor.Check(
                "repository allowlist",
                "fail",
                "no repositories allowed; run `ai-orchestrator init` then list your repositories",
            ),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["doctor"])

    assert excinfo.value.code == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == (
        "[fail] repository allowlist: no repositories allowed; "
        "run `ai-orchestrator init` then list your repositories"
    )
