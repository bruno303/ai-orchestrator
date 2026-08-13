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


class DegenerateOutputError(OpenCodeError):
    pass


@dataclass
class OpenCodeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def detect_loop(
    lines: list[str],
    window: int,
    repeat_threshold: int,
    ratio_threshold: float,
) -> bool:
    """True if the last `window` lines look like degenerate repeated output."""
    sample = [line.rstrip() for line in lines[-window:]]
    if not sample:
        return False
    counts: dict[str, int] = {}
    for line in sample:
        counts[line] = counts.get(line, 0) + 1
    if max(counts.values()) >= repeat_threshold:
        return True
    return len(counts) / len(sample) <= ratio_threshold


def run_opencode(
    workspace: str | Path,
    agent: str,
    prompt: str,
    *,
    timeout: int | None = None,
    log_file: Path | None = None,
    model: str | None = None,
    variant: str | None = None,
) -> OpenCodeResult:
    """Run `opencode run --agent <agent> --auto` in the given workspace.

    Output is streamed live to `log_file` (if given) while also captured for the
    returned result. `model`/`variant` are passed through as `-m`/`--variant`.
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
    ]
    if model is not None:
        cmd += ["-m", model]
    if variant is not None:
        cmd += ["--variant", variant]
    cmd.append(prompt)
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
        header = f"[orchestrator] opencode run --agent {agent}"
        if model is not None:
            header += f" --model {model}"
        if variant is not None:
            header += f" --variant {variant}"
        fh.write(header + "\n")
        fh.flush()
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
            if len(lines) % config.LOOP_CHECK_INTERVAL == 0 and detect_loop(
                lines,
                config.LOOP_REPEAT_WINDOW,
                config.LOOP_REPEAT_THRESHOLD,
                config.LOOP_RATIO_THRESHOLD,
            ):
                proc.kill()
                proc.wait()
                if fh is not None:
                    fh.write("[orchestrator] degenerate output detected, killing\n")
                    fh.flush()
                raise DegenerateOutputError(f"degenerate output detected after {len(lines)} lines")
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