"""Wrappers around the Codex CLI provider."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.application.ports import ExecutorError, ExecutionRequest, ExecutionResult, ReviewRequest, TriageRequest
from orchestrator.domain import ReviewOutcome
from orchestrator.infra.review.parser import parse_review_output
from orchestrator.infra.triage.parser import parse_triage_output
from orchestrator.infra.sandbox import SandboxError, SandboxRunner


class CodexError(ExecutorError):
    pass


@dataclass
class CodexResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _config_override(name: str, value: str) -> str:
    return f"{name}={json.dumps(value)}"


class CodexExecutor:
    """Execute issue phases through ``codex exec``."""

    provider_type = "codex"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        options = {**self.options, **dict(request.context.namespace("codex"))}
        log_value = request.log_file or options.get("log_file")
        try:
            result = run_codex(
                workspace=request.workspace,
                agent=request.agent,
                prompt=request.prompt,
                log_file=Path(log_value) if log_value else None,
                model=request.model,
                variant=request.variant,
                timeout=options.get("timeout"),
                sandbox=options.get("sandbox", "workspace-write"),
                approval_policy=options.get("approval_policy", "never"),
                runner=self.sandbox_runner or options.get("sandbox_runner"),
            )
        except CodexError as exc:
            raise ExecutorError(str(exc)) from exc
        return ExecutionResult(
            success=result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            context=request.context,
        )


class CodexReviewExecutor:
    """Run a read-only Codex review and validate its structured response."""

    provider_type = "codex"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        options = {**self.options, **dict(request.context.namespace("codex"))}
        model_config = options.get("model_config")
        log_value = request.log_file or options.get("log_file")
        result = run_codex(
            request.workspace,
            None,
            request.prompt,
            log_file=Path(log_value) if log_value else None,
            model=options.get("model") or (model_config.name if model_config else None),
            variant=options.get("variant") or (model_config.variant if model_config else None),
            timeout=options.get("timeout"),
            sandbox=options.get("sandbox", "read-only"),
            approval_policy=options.get("approval_policy", "never"),
            runner=self.sandbox_runner or options.get("sandbox_runner"),
        )
        if result.exit_code != 0:
            return ReviewOutcome(
                False,
                summary=result.stdout or result.stderr,
                context=request.context.merge_namespace("codex", {"exit_code": result.exit_code}),
            )
        return parse_review_output(result.stdout, request.context)


class CodexTriageExecutor:
    """Run a read-only Codex triage assessment."""

    provider_type = "codex"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

    def execute(self, request: TriageRequest):
        options = {**self.options, **dict(request.context.namespace("codex"))}
        model_config = options.get("model_config")
        result = run_codex(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else request.model),
            variant=options.get("variant") or (model_config.variant if model_config else request.variant),
            timeout=options.get("timeout"),
            sandbox=options.get("sandbox", "read-only"),
            approval_policy=options.get("approval_policy", "never"),
            runner=self.sandbox_runner or options.get("sandbox_runner"),
        )
        if result.exit_code != 0:
            return parse_triage_output("", request.context.merge_namespace("codex", {"exit_code": result.exit_code}))
        return parse_triage_output(result.stdout, request.context)


def run_codex(
    workspace: str | Path,
    agent: str | None,
    prompt: str,
    *,
    timeout: int | None = None,
    log_file: Path | None = None,
    model: str | None = None,
    variant: str | None = None,
    sandbox: str = "workspace-write",
    approval_policy: str = "never",
    runner: SandboxRunner | None = None,
) -> CodexResult:
    """Run ``codex exec`` in a workspace while streaming its output."""
    workspace = Path(workspace)
    if not workspace.exists():
        raise CodexError(f"workspace does not exist: {workspace}")

    cmd = [
        "codex",
        "exec",
        "--cd",
        "/workspace",
        "--sandbox",
        sandbox,
        "-c",
        _config_override("approval_policy", approval_policy),
    ]
    if model is not None:
        cmd += ["-m", model]
    if variant is not None:
        cmd += ["-c", _config_override("model_reasoning_effort", variant)]
    cmd.append(prompt)

    timeout = int(timeout or os.environ.get("ORCHESTRATOR_CODEX_TIMEOUT", str(60 * 60)))
    try:
        header = f"[orchestrator] codex exec --cd /workspace --sandbox {sandbox}"
        header += f" -c approval_policy={approval_policy}"
        if agent is not None:
            header += f" agent={agent}"
        if model is not None:
            header += f" --model {model}"
        if variant is not None:
            header += f" -c model_reasoning_effort={variant}"
        result = (runner or SandboxRunner()).run(
            cmd, workspace, timeout=timeout, log_file=log_file, log_header=header
        )
    except SandboxError as exc:
        raise CodexError(str(exc)) from exc

    return CodexResult(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
    )
