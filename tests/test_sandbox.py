"""Tests for the provider-neutral container runner."""

from io import StringIO
import os
from pathlib import Path

import pytest

from orchestrator.infra.sandbox.runner import SandboxError, run_sandbox


def test_runner_builds_restricted_container_command(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 0
        stdout = StringIO("ok\n")

        def wait(self):
            return None

    monkeypatch.setenv("TOKEN", "secret")
    monkeypatch.setenv("NOT_ALLOWED", "must-not-leak")
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.run", lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": ""})())
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", lambda command, **kwargs: (calls.append((command, kwargs)) or Process()))
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: (streams, [], []))

    result = run_sandbox(["agent", "--prompt", "hello"], tmp_path, environment_allowlist=["TOKEN"])

    command = calls[0][0]
    assert result.stdout == "ok\n"
    assert command[0:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "--user" in command
    assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "--network" in command and command[command.index("--network") + 1] == "bridge"
    assert "TOKEN=secret" in command
    assert "NOT_ALLOWED=must-not-leak" not in command
    assert command.count("--env") == 5
    assert "HOME=/workspace/.home" in command
    assert "XDG_CONFIG_HOME=/workspace/.config" in command
    assert "XDG_CACHE_HOME=/workspace/.cache" in command
    assert "XDG_DATA_HOME=/workspace/.local/share" in command
    mount = command[command.index("--mount") + 1]
    assert mount.startswith(f"type=bind,source={tmp_path.resolve()},target=/workspace")
    assert command.count("--mount") == 1
    assert "/workspace" in mount and command[-3:] == ["agent", "--prompt", "hello"]


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_runner_rejects_invalid_workspace(tmp_path, path_kind):
    workspace = tmp_path / "workspace"
    if path_kind == "file":
        workspace.write_text("not a directory")
    else:
        workspace = Path(workspace)
    with pytest.raises(SandboxError, match="workspace"):
        run_sandbox(["true"], workspace)


def test_runner_fails_closed_when_image_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.run", lambda *args, **kwargs: type("R", (), {"returncode": 1, "stderr": "not found"})())
    with pytest.raises(SandboxError, match="image unavailable"):
        run_sandbox(["true"], tmp_path)


def test_runner_fails_closed_when_runtime_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: None)

    with pytest.raises(SandboxError, match="runtime not found"):
        run_sandbox(["true"], tmp_path, runtime="podman")


def test_runner_fails_closed_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.shutil.which",
        lambda name: (_ for _ in ()).throw(AssertionError("runtime lookup must not occur")),
    )

    with pytest.raises(SandboxError, match="host execution is not permitted"):
        run_sandbox(["true"], tmp_path, enabled=False)


def test_runner_preserves_exit_code_and_writes_streamed_output(tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "sandbox.log"

    class Process:
        returncode = 7
        stdout = StringIO("failure\n")

        def wait(self):
            return None

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: (streams, [], []))

    result = run_sandbox(["true"], tmp_path, log_file=log_file)

    assert result.exit_code == 7
    assert result.stdout == "failure\n"
    assert "docker run --image orchestrator-agent:latest" in log_file.read_text()
    assert "failure\n" in log_file.read_text()


def test_runner_kills_process_on_timeout(tmp_path, monkeypatch):
    class Process:
        returncode = -9
        stdout = StringIO()
        killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return None

    process = Process()
    cleanup_calls = []
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.run",
        lambda *args, **kwargs: (cleanup_calls.append(args[0]) or type("R", (), {"returncode": 0, "stderr": ""})()),
    )
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: ([], [], []))

    with pytest.raises(SandboxError, match="timed out"):
        run_sandbox(["true"], tmp_path, timeout=1)
    assert process.killed is True
    assert cleanup_calls[1][0:2] == ["/usr/bin/docker", "stop"]
    assert cleanup_calls[2][0:2] == ["/usr/bin/docker", "rm"]
    assert cleanup_calls[1][2] == cleanup_calls[2][3]


def test_runner_rejects_log_setup_before_start(tmp_path, monkeypatch):
    started = False

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": ""})(),
    )

    def fail_log_open(path, *args, **kwargs):
        if path == log_file:
            raise PermissionError("log is not writable")
        return original_open(path, *args, **kwargs)

    def fail_start(*args, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("runtime must not start")

    log_file = tmp_path / "logs" / "sandbox.log"
    original_open = Path.open
    monkeypatch.setattr(Path, "open", fail_log_open)
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", fail_start)
    with pytest.raises(PermissionError):
        run_sandbox(["true"], tmp_path, log_file=log_file)
    assert started is False


def test_runner_uses_configured_network_and_no_environment_by_default(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 0
        stdout = StringIO()

        def wait(self):
            return None

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": ""})(),
    )
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.Popen",
        lambda command, **kwargs: (calls.append(command) or Process()),
    )
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: (streams, [], []))

    run_sandbox(["true"], tmp_path, network="none")

    command = calls[0]
    assert command[command.index("--network") + 1] == "none"
    assert "TOKEN=" not in " ".join(command)
    assert "HOME=/workspace/.home" in command
