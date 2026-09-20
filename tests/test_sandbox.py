"""Tests for the provider-neutral container runner."""

from io import StringIO
import os
from pathlib import Path

import pytest

from orchestrator.infra.sandbox.runner import SandboxError, SandboxMount, run_sandbox


def _runtime_ok(monkeypatch, calls, *, stdout="ok\n", stderr=""):
    class Process:
        returncode = 0

        def __init__(self):
            self.stdout = StringIO(stdout)
            self.stderr = StringIO(stderr)
            self.killed = False

        def wait(self):
            return None

        def kill(self):
            self.killed = True

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")

    def fake_run(args, **kwargs):
        calls.append(("run", args, kwargs))
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.run", fake_run)
    process = Process()
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.Popen",
        lambda command, **kwargs: (calls.append(("popen", command, kwargs)) or process),
    )
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: (streams, [], []))
    return process


def _container_command(calls):
    return next(call[1] for call in calls if call[0] == "popen")


def test_runner_builds_hardened_container_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("TOKEN", "secret")
    monkeypatch.setenv("NOT_ALLOWED", "must-not-leak")
    _runtime_ok(monkeypatch, calls)

    result = run_sandbox(
        ["agent", "--prompt", "hello"],
        tmp_path,
        environment_allowlist=["TOKEN"],
        docker_socket="/var/run/docker.sock",
    )

    command = _container_command(calls)
    workspace = str(tmp_path.resolve())
    assert result.stdout == "ok\n"
    assert command[0:3] == ["/usr/bin/docker", "run", "--rm"]
    assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert command[command.index("--pids-limit") + 1] == "512"
    assert command[command.index("--memory") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "4"
    assert command[command.index("--workdir") + 1] == workspace
    assert f"type=bind,source={workspace},target={workspace}" in command
    assert "TOKEN=secret" in command
    assert "NOT_ALLOWED=must-not-leak" not in command
    assert "HOME=/home/agent" in command
    assert "XDG_CACHE_HOME=/tmp/cache" in command
    assert any(value.startswith("COMPOSE_PROJECT_NAME=ai-") for value in command)
    assert not (tmp_path / ".home").exists()
    assert not (tmp_path / ".cache").exists()


def test_runner_rewrites_legacy_workspace_argument_for_host_docker_paths(tmp_path, monkeypatch):
    calls = []
    _runtime_ok(monkeypatch, calls)

    run_sandbox(["codex", "exec", "--cd", "/workspace", "prompt"], tmp_path)

    command = _container_command(calls)
    assert "/workspace" not in command[-5:]
    assert str(tmp_path.resolve()) in command[-5:]


def test_runner_mounts_only_explicit_provider_state_read_only(tmp_path, monkeypatch):
    calls = []
    state = tmp_path / "host-opencode"
    state.mkdir()
    _runtime_ok(monkeypatch, calls)

    run_sandbox(
        ["opencode", "run", "prompt"],
        tmp_path,
        mounts=[SandboxMount(state, "/home/agent/.config/opencode", True)],
    )

    command = _container_command(calls)
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert f"type=bind,source={state.resolve()},target=/home/agent/.config/opencode,readonly" in mounts


def test_runner_mounts_host_docker_socket_and_adds_socket_group(tmp_path, monkeypatch):
    calls = []
    socket = tmp_path / "docker.sock"
    socket.touch()
    socket_gid = socket.stat().st_gid
    _runtime_ok(monkeypatch, calls)

    run_sandbox(["docker", "version"], tmp_path, docker_socket=str(socket))

    command = _container_command(calls)
    assert command[command.index("--group-add") + 1] == str(socket_gid)
    assert f"type=bind,source={socket},target=/var/run/docker.sock,readonly" in command


def test_runner_can_disable_host_docker_socket(tmp_path, monkeypatch):
    calls = []
    _runtime_ok(monkeypatch, calls)

    run_sandbox(["true"], tmp_path, docker_socket=None)

    command = _container_command(calls)
    assert all("docker.sock" not in value for value in command)


def test_runner_fails_closed_when_image_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.run",
        lambda *args, **kwargs: type("R", (), {"returncode": 1, "stderr": "not found"})(),
    )
    with pytest.raises(SandboxError, match="image unavailable"):
        run_sandbox(["true"], tmp_path)


def test_runner_fails_closed_when_runtime_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: None)
    with pytest.raises(SandboxError, match="runtime not found"):
        run_sandbox(["true"], tmp_path, runtime="podman")


def test_runner_fails_closed_when_disabled(tmp_path):
    with pytest.raises(SandboxError, match="host execution is not permitted"):
        run_sandbox(["true"], tmp_path, enabled=False)


def test_runner_preserves_exit_code_and_streams_log(tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "sandbox.log"

    class Process:
        returncode = 7
        stdout = StringIO("failure\n")
        stderr = StringIO("diagnostic\n")

        def wait(self):
            return None

        def kill(self):
            return None

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.subprocess.run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
    )
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: (streams, [], []))

    result = run_sandbox(["true"], tmp_path, log_file=log_file, docker_socket=None)

    assert result.exit_code == 7
    assert result.stderr == "diagnostic\n"
    assert "failure\n" in log_file.read_text()


def test_runner_cleans_container_on_timeout(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = -9
        stdout = StringIO()
        stderr = StringIO()
        killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return None

    process = Process()
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")

    def fake_run(args, **kwargs):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.run", fake_run)
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.select.select", lambda streams, *_: ([], [], []))

    with pytest.raises(SandboxError, match="timed out"):
        run_sandbox(["true"], tmp_path, timeout=1, docker_socket=None)

    assert process.killed is True
    assert any(args[0:2] == ["/usr/bin/docker", "stop"] for args in calls)
    assert any(args[0:3] == ["/usr/bin/docker", "rm", "--force"] for args in calls)


def test_runner_cleans_container_on_keyboard_interrupt(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = -9
        stdout = StringIO()
        stderr = StringIO()
        killed = False

        def kill(self):
            self.killed = True

        def wait(self):
            return None

    process = Process()
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.shutil.which", lambda name: "/usr/bin/docker")

    def fake_run(args, **kwargs):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.run", fake_run)
    monkeypatch.setattr("orchestrator.infra.sandbox.runner.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        "orchestrator.infra.sandbox.runner.select.select",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        run_sandbox(["true"], tmp_path, docker_socket=None)

    assert process.killed is True
    assert any(args[0:2] == ["/usr/bin/docker", "stop"] for args in calls)
