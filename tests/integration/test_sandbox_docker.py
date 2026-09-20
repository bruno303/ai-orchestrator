"""Optional real-Docker checks for the sandbox boundary.

Build the OpenCode image first with `make build-opencode-image`. The tests skip
when Docker or the configured image is unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from orchestrator.infra.sandbox.runner import run_sandbox
from orchestrator.infra.sandbox.runner import SandboxMount


IMAGE = os.environ.get(
    "ORCHESTRATOR_DOCKER_TEST_IMAGE",
    "bruno303/ai-orchestrator-agent-opencode:latest",
)


def _image_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    result = subprocess.run(
        [docker, "image", "inspect", IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _image_available(),
    reason="Docker sandbox image is not available; run make build-opencode-image",
)


def test_real_sandbox_workspace_is_writable_and_outside_sentinel_is_hidden(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "host-sentinel"
    sentinel.write_text("secret")

    result = run_sandbox(
        [
            "sh",
            "-lc",
            f"test ! -e {sentinel} && printf ok > sandbox-output.txt",
        ],
        workspace,
        image=IMAGE,
        docker_socket=None,
    )

    assert result.exit_code == 0, result.stderr
    assert (workspace / "sandbox-output.txt").read_text() == "ok"
    assert sentinel.read_text() == "secret"


def test_real_sandbox_root_filesystem_is_read_only(tmp_path):
    result = run_sandbox(
        [
            "sh",
            "-lc",
            "awk '$2 == \"/\" && $4 ~ /(^|,)ro(,|$)/ { found=1 } END { exit !found }' /proc/mounts",
        ],
        tmp_path,
        image=IMAGE,
        docker_socket=None,
    )

    assert result.exit_code == 0, result.stderr


def test_real_opencode_starts_with_read_only_provider_state(tmp_path):
    """OpenCode must not need write access to the host provider-state mount."""
    provider_state = tmp_path / "opencode-state"
    (provider_state / "log").mkdir(parents=True)
    (provider_state / "auth.json").write_text('{"provider":"credential"}')

    result = run_sandbox(
        ["opencode", "--version"],
        tmp_path,
        image=IMAGE,
        docker_socket=None,
        mounts=(
            SandboxMount(
                provider_state,
                "/home/agent/.local/share/opencode",
                read_only=True,
            ),
        ),
        tmpfs_mounts=("/home/agent/.local/share/opencode/log",),
        writable_copies=(
            ("/home/agent/.local/share/opencode", "/tmp/opencode/data/opencode"),
        ),
        environment={
            "OPENCODE_LOG_DIR": "/tmp/opencode/log",
            "XDG_DATA_HOME": "/tmp/opencode/data",
            "XDG_STATE_HOME": "/tmp/opencode/state",
        },
        environment_allowlist_extra=(
            "OPENCODE_LOG_DIR", "XDG_DATA_HOME", "XDG_STATE_HOME"
        ),
    )

    assert result.exit_code == 0, result.stderr


def test_real_sandbox_copies_provider_credentials_to_ephemeral_state(tmp_path):
    provider_state = tmp_path / "opencode-state"
    provider_state.mkdir()
    (provider_state / "auth.json").write_text('{"provider":"credential"}')

    result = run_sandbox(
        [
            "sh",
            "-lc",
            "test -f /tmp/opencode/data/opencode/auth.json && "
            "test \"$(cat /tmp/opencode/data/opencode/auth.json)\" = '{\"provider\":\"credential\"}'",
        ],
        tmp_path,
        image=IMAGE,
        docker_socket=None,
        mounts=(
            SandboxMount(
                provider_state,
                "/home/agent/.local/share/opencode",
                read_only=True,
            ),
        ),
        writable_copies=(
            ("/home/agent/.local/share/opencode", "/tmp/opencode/data/opencode"),
        ),
    )

    assert result.exit_code == 0, result.stderr
    assert (provider_state / "auth.json").read_text() == '{"provider":"credential"}'


def test_real_sandbox_can_reach_host_docker_when_socket_exists(tmp_path):
    socket = Path("/var/run/docker.sock")
    if not socket.exists():
        pytest.skip("host Docker socket is unavailable")

    result = run_sandbox(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        tmp_path,
        image=IMAGE,
        docker_socket=str(socket),
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip()
