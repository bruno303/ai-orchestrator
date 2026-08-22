"""Wrapper around `opencode run` (PLAN.md section 9)."""

from __future__ import annotations

import select
import subprocess
import time
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.domain import ReviewCheck, ReviewFinding, ReviewOutcome
from orchestrator.application.ports import ExecutorError, ExecutionRequest, ExecutionResult, ReviewRequest


class OpenCodeError(ExecutorError):
    pass


class DegenerateOutputError(OpenCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


@dataclass
class OpenCodeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _find_opencode() -> str:
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in (Path.home() / ".opencode" / "bin" / "opencode", Path.home() / ".local" / "bin" / "opencode"):
        if candidate.is_file():
            return str(candidate)
    return "opencode"


class OpenCodeExecutor:
    """Executor implementation backed by the existing OpenCode wrapper."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        try:
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
        except DegenerateOutputError as exc:
            raise ExecutorError(str(exc), retryable=True) from exc
        except OpenCodeError as exc:
            raise ExecutorError(str(exc)) from exc
        return ExecutionResult(
            success=result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            context=request.context,
        )


class OpenCodeReviewExecutor:
    """Run the default agent and admit only the documented JSON result."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.provider_type = "opencode"

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        model_config = options.get("model_config")
        result = run_opencode(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else None),
            variant=options.get("variant") or (model_config.variant if model_config else None),
            timeout=options.get("timeout"),
            detect_degenerate=options.get("detect_degenerate", options.get("fallback_enabled", False)),
        )
        if result.exit_code != 0:
            return ReviewOutcome(False, summary=result.stdout or result.stderr,
                                 context=request.context.merge_namespace("opencode", {"exit_code": result.exit_code}))
        try:
            value = _extract_review_json(result.stdout)
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
            typed_findings = tuple(ReviewFinding(**finding) for finding in findings)
            typed_checks = tuple(ReviewCheck(**check) for check in checks)
            return ReviewOutcome(True, verdict=verdict, summary=str(value.get("summary", "")),
                                 findings=typed_findings, checks=typed_checks, context=request.context)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return ReviewOutcome(False, summary=f"invalid structured review output: {exc}", context=request.context)


def _extract_review_json(output: str) -> dict[str, Any]:
    """Extract the last JSON object from OpenCode's mixed transcript output."""
    decoder = json.JSONDecoder()
    value: dict[str, Any] | None = None
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "verdict" in candidate:
            value = candidate
    if value is None:
        raise ValueError("review output does not contain a JSON object")
    return value


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
    agent: str | None,
    prompt: str,
    *,
    timeout: int | None = None,
    log_file: Path | None = None,
    model: str | None = None,
    variant: str | None = None,
    detect_degenerate: bool = True,
) -> OpenCodeResult:
    """Run `opencode run [--agent <agent>] --auto` in the given workspace.

    Output is streamed live to `log_file` (if given) while also captured for the
    returned result. `model`/`variant` are passed through as `-m`/`--variant`.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise OpenCodeError(f"workspace does not exist: {workspace}")
    cmd = [
        os.environ.get("ORCHESTRATOR_OPENCODE_BIN") or _find_opencode(),
        "run",
        "--auto",
        "--dir",
        str(workspace),
    ]
    if agent is not None:
        cmd[2:2] = ["--agent", agent]
    if model is not None:
        cmd += ["-m", model]
    if variant is not None:
        cmd += ["--variant", variant]
    cmd.append(prompt)
    timeout = timeout or int(os.environ.get("ORCHESTRATOR_OPENCODE_TIMEOUT", str(60 * 60)))
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
        raise OpenCodeError(f"opencode binary not found: {cmd[0]}") from exc

    lines: list[str] = []
    fh = None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = log_file.open("a")
        header = "[orchestrator] opencode run"
        if agent is not None:
            header += f" --agent {agent}"
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
            if detect_degenerate and len(lines) % int(os.environ.get("ORCHESTRATOR_LOOP_CHECK_INTERVAL", "25")) == 0 and detect_loop(
                lines,
                int(os.environ.get("ORCHESTRATOR_LOOP_REPEAT_WINDOW", "100")),
                int(os.environ.get("ORCHESTRATOR_LOOP_REPEAT_THRESHOLD", "20")),
                float(os.environ.get("ORCHESTRATOR_LOOP_RATIO_THRESHOLD", "0.1")),
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
