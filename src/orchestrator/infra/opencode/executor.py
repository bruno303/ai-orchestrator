"""Wrapper around `opencode run` (PLAN.md section 9)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.domain import ReviewOutcome
from orchestrator.application.ports import ExecutorError, ExecutionRequest, ExecutionResult, ReviewRequest, TriageRequest
from orchestrator.infra.review.parser import extract_review_json, parse_review_output
from orchestrator.infra.triage.parser import parse_triage_output
from orchestrator.infra.sandbox import SandboxError, SandboxRunner


_extract_review_json = extract_review_json


class OpenCodeError(ExecutorError):
    pass


# Triage receives issue text and returns JSON; it never needs to modify the
# temporary directory or invoke tools with side effects.  The explicit deny
# rules remain effective when ``run_opencode`` uses ``--auto``.
OPENCODE_TRIAGE_CONFIG_CONTENT = json.dumps(
    {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "deny",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "lsp": "allow",
            "bash": "deny",
            "edit": "deny",
            "task": "deny",
            "skill": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "external_directory": "deny",
            "question": "deny",
            "doom_loop": "deny",
        },
    },
    separators=(",", ":"),
    sort_keys=True,
)


@dataclass
class OpenCodeResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


class OpenCodeExecutor:
    """Executor implementation backed by the existing OpenCode wrapper."""

    provider_type = "opencode"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

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
                runner=self.sandbox_runner or options.get("sandbox_runner"),
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
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        model_config = options.get("model_config")
        result = run_opencode(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else None),
            variant=options.get("variant") or (model_config.variant if model_config else None),
            timeout=options.get("timeout"),
            runner=self.sandbox_runner or options.get("sandbox_runner"),
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
        self.sandbox_runner = self.options.pop("sandbox_runner", None)

    def execute(self, request: TriageRequest):
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        model_config = options.get("model_config")
        result = run_opencode(
            request.workspace, None, request.prompt,
            log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
            model=options.get("model") or (model_config.name if model_config else request.model),
            variant=options.get("variant") or (model_config.variant if model_config else request.variant),
            timeout=options.get("timeout"),
            config_content=OPENCODE_TRIAGE_CONFIG_CONTENT,
            runner=self.sandbox_runner or options.get("sandbox_runner"),
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
    config_content: str | None = None,
    runner: SandboxRunner | None = None,
) -> OpenCodeResult:
    """Run `opencode run [--agent <agent>] --auto` in the given workspace.

    Output is streamed live to `log_file` (if given) while also captured for the
    returned result. `model`/`variant` are passed through as `-m`/`--variant`.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise OpenCodeError(f"workspace does not exist: {workspace}")
    cmd = [
        "opencode",
        "run",
        "--auto",
        "--dir",
        "/workspace",
    ]
    if agent is not None:
        cmd[2:2] = ["--agent", agent]
    if model is not None:
        cmd += ["-m", model]
    if variant is not None:
        cmd += ["--variant", variant]
    cmd.append(prompt)
    timeout = timeout or int(os.environ.get("ORCHESTRATOR_OPENCODE_TIMEOUT", str(60 * 60)))
    if config_content is not None:
        environment = {"OPENCODE_CONFIG_CONTENT": config_content}
    else:
        environment = None
    try:
        header = "[orchestrator] opencode run"
        if agent is not None:
            header += f" --agent {agent}"
        if model is not None:
            header += f" --model {model}"
        if variant is not None:
            header += f" --variant {variant}"
        result = (runner or SandboxRunner()).run(
            cmd, workspace, timeout=timeout, log_file=log_file,
            environment=environment,
            environment_allowlist_extra=("OPENCODE_CONFIG_CONTENT",) if config_content else (),
            log_header=header,
        )
    except SandboxError as exc:
        raise OpenCodeError(str(exc)) from exc
    return OpenCodeResult(
        exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr,
        duration_seconds=result.duration_seconds,
    )
