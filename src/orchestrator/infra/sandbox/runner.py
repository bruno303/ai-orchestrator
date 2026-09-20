"""Provider-neutral Docker/Podman command runner."""

from __future__ import annotations

import codecs
import io
import os
import select
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_WRITABLE_CLI_ENVIRONMENT = (
    ("HOME", "/home/agent"),
    ("XDG_CONFIG_HOME", "/home/agent/.config"),
    ("XDG_CACHE_HOME", "/tmp/cache"),
    ("XDG_DATA_HOME", "/home/agent/.local/share"),
)


class SandboxError(RuntimeError):
    """The sandbox could not be started or completed."""


@dataclass(frozen=True)
class SandboxMount:
    """One explicitly allowed host bind mount."""

    source: Path
    target: str
    read_only: bool = True


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _drain_pipe(pipe, decoder: io.IncrementalNewlineDecoder | None) -> tuple[str, bool]:
    """Read available output without waiting for a line delimiter."""
    try:
        file_descriptor = pipe.fileno()
    except (AttributeError, io.UnsupportedOperation):
        chunk = pipe.readline()
        return chunk, not chunk
    chunk = os.read(file_descriptor, 65536)
    if chunk:
        assert decoder is not None
        return decoder.decode(chunk), False
    if decoder is None:
        return "", True
    return decoder.decode(b"", final=True), True


def _decoder_for_pipe(pipe) -> io.IncrementalNewlineDecoder | None:
    encoding = getattr(pipe, "encoding", None)
    if encoding is None:
        return None
    return io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder(encoding)(errors=getattr(pipe, "errors", None) or "strict"),
        translate=True,
    )


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


def _mount_args(mounts: Sequence[SandboxMount]) -> list[str]:
    args: list[str] = []
    for mount in mounts:
        source = Path(mount.source)
        if not source.exists():
            continue
        spec = f"type=bind,source={source.resolve()},target={mount.target}"
        if mount.read_only:
            spec += ",readonly"
        args += ["--mount", spec]
    return args


def _docker_socket_args(runtime: str, docker_socket: str | None) -> list[str]:
    if runtime != "docker" or not docker_socket:
        return []
    args = [
        "--mount",
        f"type=bind,source={docker_socket},target=/var/run/docker.sock,readonly",
    ]
    try:
        socket_gid = os.stat(docker_socket).st_gid
    except OSError:
        return args
    return ["--group-add", str(socket_gid), *args]


def _tmpfs_args(targets: Sequence[str], uid: int, gid: int) -> list[str]:
    args: list[str] = []
    for target in targets:
        args += ["--tmpfs", f"{target}:rw,nosuid,nodev,uid={uid},gid={gid},mode=0700"]
    return args


def _writable_copy_command(
    command: Sequence[str], copies: Sequence[tuple[str, str]]
) -> list[str]:
    """Copy read-only provider state into ephemeral storage before execution."""
    if not copies:
        return list(command)
    lines = ["set -eu"]
    for source, target in copies:
        quoted_source = shlex.quote(source)
        quoted_target = shlex.quote(target)
        lines.extend(
            (
                f"if [ -d {quoted_source} ]; then",
                f"  mkdir -p {quoted_target}",
                f"  cp -a {quoted_source}/. {quoted_target}/",
                f"elif [ -f {quoted_source} ]; then",
                f"  mkdir -p \"$(dirname {quoted_target})\"",
                f"  cp -a {quoted_source} {quoted_target}",
                "fi",
            )
        )
    lines.append('exec "$@"')
    return ["sh", "-c", "\n".join(lines), "sandbox", *command]


def run_sandbox(
    command: Sequence[str],
    workspace: str | Path,
    *,
    runtime: str = "docker",
    image: str = "bruno303/ai-orchestrator-agent-opencode:latest",
    network: str = "bridge",
    environment_allowlist: Sequence[str] = (),
    timeout: int | None = None,
    log_file: Path | None = None,
    enabled: bool = True,
    environment: Mapping[str, str] | None = None,
    environment_allowlist_extra: Sequence[str] = (),
    log_header: str | None = None,
    mounts: Sequence[SandboxMount] = (),
    cpus: str = "4",
    memory: str = "4g",
    pids_limit: int = 512,
    docker_socket: str | None = "/var/run/docker.sock",
    tmpfs_mounts: Sequence[str] = (),
    writable_copies: Sequence[tuple[str, str]] = (),
) -> SandboxResult:
    """Run a command in a hardened container with only explicit host mounts.

    The host Docker socket is intentionally supported for builds/Compose. Its
    presence means this protects against accidental host access, not malicious
    code with intent to escape the container.
    """
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

    workspace_path = str(workspace.resolve())
    container_id = uuid.uuid4().hex
    container_name = f"orchestrator-sandbox-{container_id}"
    compose_project = f"ai-{container_id[:12]}"
    uid, gid = os.getuid(), os.getgid()

    env_args: list[str] = []
    for name, value in _WRITABLE_CLI_ENVIRONMENT:
        env_args += ["--env", f"{name}={value}"]
    env_args += ["--env", f"COMPOSE_PROJECT_NAME={compose_project}"]
    for name in (*environment_allowlist, *environment_allowlist_extra):
        value = (environment or {}).get(name, os.environ.get(name))
        if value is not None:
            env_args += ["--env", f"{name}={value}"]

    # Keep the same absolute workspace path inside the agent container. Docker
    # Compose talks to the host daemon through docker.sock, so host-side bind
    # mounts referenced by Compose must resolve to the same path.
    command = [workspace_path if value == "/workspace" else value for value in command]
    command = _writable_copy_command(command, writable_copies)

    cmd = [
        binary,
        "run",
        "--rm",
        "--name",
        container_name,
        "--user",
        f"{uid}:{gid}",
        "--network",
        network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,mode=1777",
        "--tmpfs",
        f"/home/agent:rw,nosuid,nodev,uid={uid},gid={gid},mode=0700",
        *_tmpfs_args(tmpfs_mounts, uid, gid),
        "--workdir",
        workspace_path,
        "--mount",
        f"type=bind,source={workspace_path},target={workspace_path}",
        *_mount_args(mounts),
        *_docker_socket_args(runtime, docker_socket),
        *env_args,
        image,
        *command,
    ]

    timeout = int(timeout or 60 * 60)
    start = time.monotonic()
    fh = None
    proc = None
    completed = False
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
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"sandbox runtime not found: {binary}") from exc
        except OSError as exc:
            raise SandboxError(f"sandbox could not start with runtime {binary}: {exc}") from exc

        assert proc.stdout is not None
        pipes = {proc.stdout: "stdout"}
        stderr_pipe = getattr(proc, "stderr", None)
        if stderr_pipe is not None:
            pipes[stderr_pipe] = "stderr"
        decoders = {pipe: _decoder_for_pipe(pipe) for pipe in pipes}
        streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
        deadline = time.monotonic() + timeout
        active = list(pipes)
        while active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxError(f"sandbox run timed out after {timeout}s")
            readable, _, _ = select.select(active, [], [], remaining)
            if not readable:
                raise SandboxError(f"sandbox run timed out after {timeout}s")
            for pipe in readable:
                chunk, eof = _drain_pipe(pipe, decoders[pipe])
                if chunk:
                    streams[pipes[pipe]].append(chunk)
                    if fh is not None:
                        fh.write(chunk)
                        fh.flush()
                if eof:
                    active.remove(pipe)
        proc.wait()
        completed = True
        return SandboxResult(
            proc.returncode,
            "".join(streams["stdout"]),
            "".join(streams["stderr"]),
            time.monotonic() - start,
        )
    finally:
        if proc is not None:
            if not completed:
                try:
                    proc.kill()
                    proc.wait()
                except (OSError, AttributeError):
                    pass
            _cleanup_container(binary, container_name)
        if runtime == "docker" and docker_socket and Path(docker_socket).exists():
            _cleanup_compose_resources(binary, compose_project)
        if fh is not None:
            fh.close()


def _cleanup_container(binary: str, container_name: str) -> None:
    """Stop and remove a container whose runtime client exited or was killed."""
    for args in (
        [binary, "stop", container_name],
        [binary, "rm", "--force", container_name],
    ):
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            continue


def _cleanup_compose_resources(binary: str, project_name: str) -> None:
    """Best-effort cleanup limited to resources created by this Compose project."""
    label = f"label=com.docker.compose.project={project_name}"
    resources = (
        ([binary, "ps", "-aq", "--filter", label], [binary, "rm", "-f"]),
        ([binary, "network", "ls", "-q", "--filter", label], [binary, "network", "rm"]),
        ([binary, "volume", "ls", "-q", "--filter", label], [binary, "volume", "rm", "-f"]),
    )
    for list_command, remove_command in resources:
        try:
            listed = subprocess.run(
                list_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            ids = (listed.stdout or "").split()
            if ids:
                subprocess.run(
                    [*remove_command, *ids],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        except OSError:
            continue


class SandboxRunner:
    """Reusable runner carrying one sandbox configuration."""

    def __init__(self, **options) -> None:
        self.options = dict(options)

    def run(self, command: Sequence[str], workspace: str | Path, **options) -> SandboxResult:
        return run_sandbox(command, workspace, **{**self.options, **options})
