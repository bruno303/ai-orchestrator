"""Wrappers around the Claude Code CLI provider."""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.application.ports import ExecutorError, ExecutionRequest, ExecutionResult, ReviewRequest
from orchestrator.domain import ReviewOutcome
from orchestrator.infra.review.parser import parse_review_output


class ClaudeError(ExecutorError):
    """Failure while invoking the Claude Code CLI."""


@dataclass
class ClaudeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _find_claude() -> str:
    """Resolve the Claude Code binary from PATH and common local locations."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".claude" / "bin" / "claude",
        Path.home() / ".local" / "bin" / "claude",
    ):
        if candidate.is_file():
            return str(candidate)
    return "claude"


class ClaudeExecutor:
    """Execute issue phases through Claude Code's non-interactive mode."""

    provider_type = "claude"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        options = {**self.options, **dict(request.context.namespace("claude"))}
        log_value = request.log_file or options.get("log_file")
        try:
            result = run_claude(
                workspace=request.workspace,
                agent=request.agent,
                prompt=request.prompt,
                log_file=Path(log_value) if log_value else None,
                model=options.get("model") or request.model,
                variant=options.get("variant") or request.variant,
                timeout=options.get("timeout"),
                permission_mode=options.get("permission_mode"),
            )
        except ClaudeError as exc:
            raise ExecutorError(str(exc)) from exc
        return ExecutionResult(
            success=result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            context=request.context,
        )


class ClaudeReviewExecutor:
    """Run Claude Code in its read-only planning mode for pull-request reviews."""

    provider_type = "claude"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        options = {**self.options, **dict(request.context.namespace("claude"))}
        model_config = options.get("model_config")
        log_value = request.log_file or options.get("log_file")
        result = run_claude(
            request.workspace,
            None,
            request.prompt,
            log_file=Path(log_value) if log_value else None,
            model=options.get("model") or (model_config.name if model_config else None),
            variant=options.get("variant") or (model_config.variant if model_config else None),
            timeout=options.get("timeout"),
            permission_mode=options.get("permission_mode") or "plan",
        )
        if result.exit_code != 0:
            return ReviewOutcome(
                False,
                summary=result.stdout or result.stderr,
                context=request.context.merge_namespace("claude", {"exit_code": result.exit_code}),
            )
        return parse_review_output(result.stdout, request.context)


def run_claude(
    workspace: str | Path,
    agent: str | None,
    prompt: str,
    *,
    timeout: int | None = None,
    log_file: Path | None = None,
    model: str | None = None,
    variant: str | None = None,
    permission_mode: str | None = None,
) -> ClaudeResult:
    """Run Claude Code in a workspace while streaming its text output.

    The generic ``agent`` argument is intentionally not forwarded: Claude Code
    custom agents are deployment-specific, while the orchestrator's prompts
    already describe each plan/build/test phase. Claude Code 2.1.19 does not
    accept an ``--effort`` CLI option, so a configured variant is exposed to
    the child process through ``CLAUDE_CODE_EFFORT_LEVEL`` instead.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise ClaudeError(f"workspace does not exist: {workspace}")

    cmd = [
        os.environ.get("ORCHESTRATOR_CLAUDE_BIN") or _find_claude(),
        "-p",
        "--output-format",
        "text",
    ]
    if model is not None:
        cmd += ["--model", model]
    if permission_mode is not None:
        cmd += ["--permission-mode", permission_mode]
    cmd.append(prompt)

    child_environment = os.environ.copy()
    if variant is not None:
        child_environment["CLAUDE_CODE_EFFORT_LEVEL"] = variant

    timeout = int(timeout or os.environ.get("ORCHESTRATOR_CLAUDE_TIMEOUT", str(60 * 60)))
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise ClaudeError(f"claude binary not found: {cmd[0]}") from exc

    lines: list[str] = []
    fh = None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = log_file.open("a")
        header = "[orchestrator] claude -p --output-format text"
        if model is not None:
            header += f" --model {model}"
        if variant is not None:
            header += f" variant={variant}"
        if permission_mode is not None:
            header += f" --permission-mode {permission_mode}"
        if agent is not None:
            header += f" agent={agent}"
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
                raise ClaudeError(f"claude run timed out after {timeout}s")
            readable, _, _ = select.select([proc.stdout], [], [], remaining)
            if not readable:
                proc.kill()
                proc.wait()
                raise ClaudeError(f"claude run timed out after {timeout}s")
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
        raise ClaudeError(f"claude run timed out after {timeout}s") from exc
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        raise
    finally:
        if fh is not None:
            fh.close()

    return ClaudeResult(
        exit_code=proc.returncode,
        stdout="".join(lines),
        stderr="",
        duration_seconds=time.monotonic() - start,
    )
