"""Provider-neutral Docker/Podman command runner."""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_WRITABLE_CLI_ENVIRONMENT = (
    ("HOME", "/workspace/.home"),
    ("XDG_CONFIG_HOME", "/workspace/.config"),
    ("XDG_CACHE_HOME", "/workspace/.cache"),
    ("XDG_DATA_HOME", "/workspace/.local/share"),
)


class SandboxError(RuntimeError):
    """The sandbox could not be started or completed."""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _runtime_binary(runtime: str) -> str:
    if runtime not in {"docker", "podman"}:
        raise SandboxError(f"unsupported sandbox runtime: {runtime}")
    binary = shutil.which(runtime)
    if not binary:
        raise SandboxError(f"sandbox runtime not found: {runtime}")
    return binary


def _check_image(binary: str, image: str) -> None:
    try:
        result = subprocess.run(
            [binary, "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SandboxError(f"sandbox runtime unavailable: {binary}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise SandboxError(f"sandbox image unavailable: {image}{suffix}")


def run_sandbox(
    command: Sequence[str],
    workspace: str | Path,
    *,
    runtime: str = "docker",
    image: str = "orchestrator-agent:latest",
    network: str = "bridge",
    environment_allowlist: Sequence[str] = (),
    timeout: int | None = None,
    log_file: Path | None = None,
    enabled: bool = True,
    environment: Mapping[str, str] | None = None,
    environment_allowlist_extra: Sequence[str] = (),
    log_header: str | None = None,
) -> SandboxResult:
    """Run a command in a workspace-only, non-root container."""
    if not enabled:
        raise SandboxError("sandboxing is disabled; host execution is not permitted")
    workspace = Path(workspace)
    if not workspace.exists():
        raise SandboxError(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise SandboxError(f"workspace is not a directory: {workspace}")
    if not image:
        raise SandboxError("sandbox image must not be empty")
    binary = _runtime_binary(runtime)
    _check_image(binary, image)

    container_name = f"orchestrator-sandbox-{uuid.uuid4().hex}"
    env_args: list[str] = []
    for name in (*environment_allowlist, *environment_allowlist_extra):
        value = (environment or {}).get(name, os.environ.get(name))
        if value is not None:
            env_args += ["--env", f"{name}={value}"]
    cmd = [
        binary,
        "run",
        "--rm",
        "--name",
        container_name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--network",
        network,
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,source={workspace.resolve()},target=/workspace,rw",
        *sum((["--env", f"{name}={value}"] for name, value in _WRITABLE_CLI_ENVIRONMENT), []),
        *env_args,
        image,
        *command,
    ]
    timeout = int(timeout or 60 * 60)
    start = time.monotonic()
    fh = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = log_file.open("a")
            fh.write((log_header or f"[orchestrator] {runtime} run --image {image}") + "\n")
            fh.flush()
        except BaseException:
            if fh is not None:
                fh.close()
            raise
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        if fh is not None:
            fh.close()
        raise SandboxError(f"sandbox runtime not found: {binary}") from exc
    except OSError as exc:
        if fh is not None:
            fh.close()
        raise SandboxError(f"sandbox could not start with runtime {binary}: {exc}") from exc

    lines: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                _cleanup_container(binary, container_name)
                raise SandboxError(f"sandbox run timed out after {timeout}s")
            readable, _, _ = select.select([proc.stdout], [], [], remaining)
            if not readable:
                proc.kill()
                proc.wait()
                _cleanup_container(binary, container_name)
                raise SandboxError(f"sandbox run timed out after {timeout}s")
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if fh is not None:
                fh.write(line)
                fh.flush()
        proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        raise
    finally:
        if fh is not None:
            fh.close()
    return SandboxResult(proc.returncode, "".join(lines), "", time.monotonic() - start)


def _cleanup_container(binary: str, container_name: str) -> None:
    """Stop and remove a container whose runtime client was terminated."""
    for args in (
        [binary, "stop", container_name],
        [binary, "rm", "--force", container_name],
    ):
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            continue


class SandboxRunner:
    """Reusable runner carrying one sandbox configuration."""

    def __init__(self, **options) -> None:
        self.options = dict(options)

    def run(self, command: Sequence[str], workspace: str | Path, **options) -> SandboxResult:
        return run_sandbox(command, workspace, **{**self.options, **options})
