"""Wrapper around `opencode run` (PLAN.md section 9)."""

from __future__ import annotations

import json
import select
import subprocess
import time
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.domain import ReviewOutcome, TriageOutcome
from orchestrator.application.ports import (
    DiscussionRequest,
    DiscussionResult,
    ExecutorError,
    ExecutionRequest,
    ExecutionResult,
    ReviewRequest,
    TriageRequest,
)
from orchestrator.infra.review.parser import extract_review_json, parse_review_output
from orchestrator.infra.triage.parser import parse_triage_output


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

# Discussion has the same read-only tool boundary as triage, but is a distinct
# provider contract so the application cannot accidentally route it through a
# planning or implementation executor.
OPENCODE_DISCUSSION_CONFIG_CONTENT = OPENCODE_TRIAGE_CONFIG_CONTENT


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


class OpenCodeDiscussionExecutor:
    """Run a discussion with OpenCode's explicit read-only permissions."""

    provider_type = "opencode"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def execute(self, request: DiscussionRequest) -> DiscussionResult:
        options = {**self.options, **dict(request.context.namespace("opencode"))}
        model_config = options.get("model_config")
        try:
            result = run_opencode(
                workspace=request.workspace,
                agent=None,
                prompt=request.prompt,
                log_file=Path(request.log_file or options["log_file"]) if request.log_file or options.get("log_file") else None,
                model=request.model or (model_config.name if model_config else None),
                variant=request.variant or (model_config.variant if model_config else None),
                timeout=options.get("timeout"),
                config_content=OPENCODE_DISCUSSION_CONFIG_CONTENT,
            )
        except OpenCodeError as exc:
            raise ExecutorError(str(exc)) from exc
        return DiscussionResult(
            success=result.exit_code == 0,
            response=result.stdout,
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
            config_content=OPENCODE_TRIAGE_CONFIG_CONTENT,
        )
        if result.exit_code != 0:
            context = request.context.merge_namespace("opencode", {"exit_code": result.exit_code})
            return TriageOutcome(
                False,
                summary=result.stdout or result.stderr or "OpenCode triage executor failed",
                context=context,
            )
        return parse_triage_output(result.stdout, request.context)


def _model_reference(model: str, variant: str | None) -> str:
    """Return the OpenCode V2 model reference without duplicating a variant."""
    if variant and "#" not in model:
        return f"{model}#{variant}"
    return model


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
) -> OpenCodeResult:
    """Run `opencode run [--agent <agent>] --auto` in the given workspace.

    Output is streamed live to `log_file` (if given) while also captured for the
    returned result. OpenCode V2 receives the model and variant as one reference.
    """
    workspace = Path(workspace)
    if not workspace.exists():
        raise OpenCodeError(f"workspace does not exist: {workspace}")
    cmd = [
        os.environ.get("ORCHESTRATOR_OPENCODE_BIN") or _find_opencode(),
        "run",
        "--auto",
    ]
    if agent is not None:
        cmd[2:2] = ["--agent", agent]
    if model is not None:
        cmd += ["-m", _model_reference(model, variant)]
    cmd.append(prompt)
    timeout = timeout or int(os.environ.get("ORCHESTRATOR_OPENCODE_TIMEOUT", str(60 * 60)))
    start = time.monotonic()
    try:
        child_environment = os.environ.copy()
        if config_content is not None:
            child_environment["OPENCODE_CONFIG_CONTENT"] = config_content
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=child_environment,
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
            header += f" --model {_model_reference(model, variant)}"
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
