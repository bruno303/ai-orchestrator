"""Wrapper around `opencode run` (PLAN.md section 9)."""

from __future__ import annotations

import select
import subprocess
import time
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.domain import ReviewOutcome
from orchestrator.application.ports import ExecutorError, ExecutionRequest, ExecutionResult, ReviewRequest, TriageRequest
from orchestrator.infra.review.parser import extract_review_json, parse_review_output
from orchestrator.infra.triage.parser import parse_triage_output


_extract_review_json = extract_review_json


class OpenCodeError(ExecutorError):
    pass


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

    provider_type = "opencode"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        try:
            result = run_opencode(
                workspace=request.workspace,
                agent=request.agent,
                prompt=request.prompt,
                log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
                model=request.model,
                variant=request.variant,
                timeout=options.get("timeout"),
            )
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

    provider_type = "opencode"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        model_config = options.get("model_config")
        result = run_opencode(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else None),
            variant=options.get("variant") or (model_config.variant if model_config else None),
            timeout=options.get("timeout"),
        )
        if result.exit_code != 0:
            return ReviewOutcome(False, summary=result.stdout or result.stderr,
                                 context=request.context.merge_namespace("opencode", {"exit_code": result.exit_code}))
        return parse_review_output(result.stdout, request.context)


class OpenCodeTriageExecutor:
    """Run a triage prompt in an ephemeral workspace."""

    provider_type = "opencode"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: TriageRequest):
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        model_config = options.get("model_config")
        result = run_opencode(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else request.model),
            variant=options.get("variant") or (model_config.variant if model_config else request.variant),
            timeout=options.get("timeout"),
        )
        if result.exit_code != 0:
            return parse_triage_output("", request.context.merge_namespace("opencode", {"exit_code": result.exit_code}))
        return parse_triage_output(result.stdout, request.context)


def run_opencode(
    workspace: str | Path,
    agent: str | None,
    prompt: str,
    *,
    timeout: int | None = None,
    log_file: Path | None = None,
    model: str | None = None,
    variant: str | None = None,
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
