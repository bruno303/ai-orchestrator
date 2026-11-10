"""Workspace command backend: orchestration-owned subprocess I/O.

Escape-hatch `command` step backend in `infra/filesystem`; unused by default
workflows. No environment reads here (composition passes values). This module
never raises: every failure is reported as an unsuccessful ExecutionResult.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from orchestrator.application.ports import ExecutionResult


def _write_log(log_file: str, stdout: str, stderr: str) -> None:
    """Best-effort write of the captured output; IO errors are swallowed."""
    if not log_file:
        return
    try:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stdout + stderr)
    except OSError:
        pass


def run_workspace_command(
    workspace: str,
    command: str,
    timeout_seconds: int,
    log_file: str = "",
) -> ExecutionResult:
    """Run one shell command inside a task workspace without raising."""
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        result = ExecutionResult(
            False,
            124,
            stderr=f"command timed out after {timeout_seconds}s",
            duration_seconds=time.monotonic() - start,
        )
    except OSError as exc:
        result = ExecutionResult(
            False,
            127,
            stderr=str(exc),
            duration_seconds=time.monotonic() - start,
        )
    except Exception as exc:
        result = ExecutionResult(
            False,
            1,
            stderr=str(exc),
            duration_seconds=time.monotonic() - start,
        )
    else:
        result = ExecutionResult(
            completed.returncode == 0,
            completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - start,
        )
    _write_log(log_file, result.stdout, result.stderr)
    return result
