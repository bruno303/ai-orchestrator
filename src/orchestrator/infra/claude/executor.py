"""Wrappers around the Claude Code CLI provider."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.application.ports import ExecutorError, ExecutionRequest, ExecutionResult, ReviewRequest, TriageRequest
from orchestrator.domain import ReviewOutcome
from orchestrator.infra.review.parser import parse_review_output
from orchestrator.infra.triage.parser import parse_triage_output
from orchestrator.infra.sandbox import SandboxError, SandboxRunner


class ClaudeError(ExecutorError):
    """Failure while invoking the Claude Code CLI."""


@dataclass
class ClaudeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class ClaudeExecutor:
    """Execute issue phases through Claude Code's non-interactive mode."""

    provider_type = "claude"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

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
                runner=self.sandbox_runner or options.get("sandbox_runner"),
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
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

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
            runner=self.sandbox_runner or options.get("sandbox_runner"),
        )
        if result.exit_code != 0:
            return ReviewOutcome(
                False,
                summary=result.stdout or result.stderr,
                context=request.context.merge_namespace("claude", {"exit_code": result.exit_code}),
            )
        return parse_review_output(result.stdout, request.context)


class ClaudeTriageExecutor:
    """Run Claude Code in read-only plan mode for issue triage."""

    provider_type = "claude"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

    def execute(self, request: TriageRequest):
        options = {**self.options, **dict(request.context.namespace("claude"))}
        model_config = options.get("model_config")
        result = run_claude(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else request.model),
            variant=options.get("variant") or (model_config.variant if model_config else request.variant),
            timeout=options.get("timeout"),
            permission_mode=options.get("permission_mode") or "plan",
            runner=self.sandbox_runner or options.get("sandbox_runner"),
        )
        if result.exit_code != 0:
            return parse_triage_output("", request.context.merge_namespace("claude", {"exit_code": result.exit_code}))
        return parse_triage_output(result.stdout, request.context)


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
    runner: SandboxRunner | None = None,
) -> ClaudeResult:
    """Run Claude Code in a workspace while streaming its text output.

    The generic ``agent`` argument is intentionally not forwarded: Claude Code
    custom agents are deployment-specific, while the orchestrator's prompts
    already describe each plan/build phase. Claude Code 2.1.19 does not
    accept an ``--effort`` CLI option, so a configured variant is exposed to
    the child process through ``CLAUDE_CODE_EFFORT_LEVEL`` instead.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise ClaudeError(f"workspace does not exist: {workspace}")

    cmd = [
        "claude",
        "-p",
        "--output-format",
        "text",
    ]
    if model is not None:
        cmd += ["--model", model]
    if permission_mode is not None:
        cmd += ["--permission-mode", permission_mode]
    cmd.append(prompt)

    timeout = int(timeout or os.environ.get("ORCHESTRATOR_CLAUDE_TIMEOUT", str(60 * 60)))
    environment = {"CLAUDE_CODE_EFFORT_LEVEL": variant} if variant is not None else None
    try:
        header = "[orchestrator] claude -p --output-format text"
        if model is not None:
            header += f" --model {model}"
        if variant is not None:
            header += f" variant={variant}"
        if permission_mode is not None:
            header += f" --permission-mode {permission_mode}"
        if agent is not None:
            header += f" agent={agent}"
        result = (runner or SandboxRunner()).run(
            cmd, workspace, timeout=timeout, log_file=log_file, environment=environment,
            environment_allowlist_extra=("CLAUDE_CODE_EFFORT_LEVEL",) if variant is not None else (),
            log_header=header,
        )
    except SandboxError as exc:
        raise ClaudeError(str(exc)) from exc

    return ClaudeResult(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
    )
