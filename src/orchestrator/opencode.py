"""Wrapper around `opencode run` (PLAN.md section 9)."""

from __future__ import annotations

import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from orchestrator import config


class OpenCodeError(Exception):
    pass


@dataclass
class OpenCodeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def run_opencode(
    workspace: str | Path,
    agent: str,
    prompt: str,
    *,
    timeout: int | None = None,
    log_file: Path | None = None,
) -> OpenCodeResult:
    """Run `opencode run --agent <agent> --auto` in the given workspace.

    Output is streamed live to `log_file` (if given) while also captured for the
    returned result.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise OpenCodeError(f"workspace does not exist: {workspace}")
    cmd = [
        config.OPENCODE_BIN,
        "run",
        "--agent",
        agent,
        "--auto",
        "--dir",
        str(workspace),
        prompt,
    ]
    timeout = timeout or config.OPENCODE_TIMEOUT_SECONDS
    start = time.monotonic()
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
        raise OpenCodeError(f"opencode binary not found: {config.OPENCODE_BIN}") from exc

    lines: list[str] = []
    fh = None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = log_file.open("a")
    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise OpenCodeError(f"opencode run timed out after {timeout}s")
            readable, _, _ = select.select([proc.stdout], [], [], remaining)
            if not readable:
                proc.kill()
                proc.wait()
                raise OpenCodeError(f"opencode run timed out after {timeout}s")
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if fh is not None:
                fh.write(line)
                fh.flush()
        proc.wait()
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise OpenCodeError(f"opencode run timed out after {timeout}s") from exc
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        raise
    finally:
        if fh is not None:
            fh.close()
    return OpenCodeResult(
        exit_code=proc.returncode,
        stdout="".join(lines),
        stderr="",
        duration_seconds=time.monotonic() - start,
    )