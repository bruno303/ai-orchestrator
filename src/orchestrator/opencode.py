"""Wrapper around `opencode run` (PLAN.md section 9)."""

from __future__ import annotations

import select
import subprocess
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator import config
from orchestrator.providers import ExecutionRequest, ExecutionResult, ReviewRequest, ReviewResult


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


class OpenCodeExecutor:
    """Executor implementation backed by the existing OpenCode wrapper."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        options = {**self.options, **request.provider_state}
        result = run_opencode(
            workspace=request.workspace,
            agent=request.agent,
            prompt=request.prompt,
            log_file=Path(options["log_file"]) if options.get("log_file") else None,
            model=request.model,
            variant=request.variant,
            timeout=options.get("timeout"),
            detect_degenerate=options.get("detect_degenerate", True),
        )
        return ExecutionResult(
            success=result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
        )


class OpenCodeReviewExecutor:
    """Run the review agent and admit only the documented JSON result."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.provider_type = "opencode"

    def execute(self, request: ReviewRequest) -> ReviewResult:
        options = {**self.options, **request.provider_state}
        model_config = config.MODEL_PRIMARY
        result = run_opencode(
            request.workspace, options.get("agent", "review"), request.prompt,
            log_file=Path(options["log_file"]) if options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else None),
            variant=options.get("variant") or (model_config.variant if model_config else None),
            timeout=options.get("timeout"), detect_degenerate=options.get("detect_degenerate", True),
        )
        if result.exit_code != 0:
            return ReviewResult(False, summary=result.stdout or result.stderr,
                                provider_state={"exit_code": result.exit_code})
        try:
            value = json.loads(result.stdout.strip())
            if not isinstance(value, dict):
                raise ValueError("review output is not an object")
            verdict = str(value.get("verdict", "")).lower()
            if verdict not in {"approve", "request_changes", "comment"}:
                raise ValueError("review verdict is invalid")
            findings = value.get("findings", [])
            checks = value.get("checks", [])
            if not isinstance(findings, list) or not isinstance(checks, list):
                raise ValueError("findings and checks must be arrays")
            if not isinstance(value.get("summary", ""), str):
                raise ValueError("summary must be a string")
            for finding in findings:
                if not isinstance(finding, dict) or not isinstance(finding.get("message"), str):
                    raise ValueError("each finding must have a message")
                if "path" in finding and not isinstance(finding["path"], str):
                    raise ValueError("finding path must be a string")
                if "line" in finding and (not isinstance(finding["line"], int) or isinstance(finding["line"], bool)):
                    raise ValueError("finding line must be an integer")
                if "start_line" in finding and (not isinstance(finding["start_line"], int) or isinstance(finding["start_line"], bool)):
                    raise ValueError("finding start_line must be an integer")
                if finding.get("side", "RIGHT") not in {"LEFT", "RIGHT"}:
                    raise ValueError("finding side is invalid")
                if finding.get("start_side", finding.get("side", "RIGHT")) not in {"LEFT", "RIGHT"}:
                    raise ValueError("finding start_side is invalid")
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check.get("name"), str) or check.get("status") not in {"pass", "fail", "skip"}:
                    raise ValueError("each check must have a name and valid status")
            return ReviewResult(True, verdict=verdict, summary=str(value.get("summary", "")),
                                comments=findings, checks=checks)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return ReviewResult(False, summary=f"invalid structured review output: {exc}")


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
    detect_degenerate: bool = True,
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
            if detect_degenerate and len(lines) % config.LOOP_CHECK_INTERVAL == 0 and detect_loop(
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
