"""Wrappers around the Codex CLI provider."""

from __future__ import annotations

import json
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


class CodexError(ExecutorError):
    pass


@dataclass
class CodexResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float


def _find_codex() -> str:
    """Resolve the Codex binary from PATH and common local install locations."""
    found = shutil.which("codex")
    if found:
        return found
    for candidate in (
        Path.home() / ".codex" / "bin" / "codex",
        Path.home() / ".local" / "bin" / "codex",
    ):
        if candidate.is_file():
            return str(candidate)
    return "codex"


def _config_override(name: str, value: str) -> str:
    return f"{name}={json.dumps(value)}"


class CodexExecutor:
    """Execute issue phases through ``codex exec``."""

    provider_type = "codex"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

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
        )
        if result.exit_code != 0:
            return ReviewOutcome(
                False,
                summary=result.stdout or result.stderr,
                context=request.context.merge_namespace("codex", {"exit_code": result.exit_code}),
            )
        return parse_review_output(result.stdout, request.context)


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
) -> CodexResult:
    """Run ``codex exec`` in a workspace while streaming its output."""
    workspace = Path(workspace)
    if not workspace.exists():
        raise CodexError(f"workspace does not exist: {workspace}")

    cmd = [
        os.environ.get("ORCHESTRATOR_CODEX_BIN") or _find_codex(),
        "exec",
        "--cd",
        str(workspace),
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
        raise CodexError(f"codex binary not found: {cmd[0]}") from exc

    lines: list[str] = []
    fh = None
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = log_file.open("a")
        header = f"[orchestrator] codex exec --cd {workspace} --sandbox {sandbox}"
        header += f" -c approval_policy={approval_policy}"
        if agent is not None:
            header += f" agent={agent}"
        if model is not None:
            header += f" --model {model}"
        if variant is not None:
            header += f" -c model_reasoning_effort={variant}"
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
                raise CodexError(f"codex exec timed out after {timeout}s")
            readable, _, _ = select.select([proc.stdout], [], [], remaining)
            if not readable:
                proc.kill()
                proc.wait()
                raise CodexError(f"codex exec timed out after {timeout}s")
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
        raise CodexError(f"codex exec timed out after {timeout}s") from exc
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        raise
    finally:
        if fh is not None:
            fh.close()

    return CodexResult(
        exit_code=proc.returncode,
        stdout="".join(lines),
        stderr="",
        duration_seconds=time.monotonic() - start,
    )
