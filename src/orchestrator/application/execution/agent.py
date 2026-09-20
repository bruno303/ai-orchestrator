"""Issue-phase agent execution policy."""

from __future__ import annotations

from datetime import datetime

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.application.ports import ExecutorError, ExecutionRequest
from orchestrator.application.execution.errors import AgentExecutionError
from orchestrator.application.execution.models import AgentRequest, PhaseResult


_FAILURE_DIAGNOSTIC_LIMIT = 2_000


def _failure_diagnostic(stdout: str, stderr: str) -> str:
    """Return a bounded, labeled excerpt of provider output."""
    streams = []
    for name, output in (("stdout", stdout), ("stderr", stderr)):
        output = output.strip()
        if output:
            streams.append(f"{name}: {output}")
    diagnostic = "\n".join(streams)
    if len(diagnostic) > _FAILURE_DIAGNOSTIC_LIMIT:
        return diagnostic[:_FAILURE_DIAGNOSTIC_LIMIT - 3].rstrip() + "..."
    return diagnostic


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass(frozen=True)
class AgentSettings:
    model: object | None = None


class IssueAgentRunner:
    """Run one issue phase with the configured model."""

    def __init__(self, executor, settings: AgentSettings = AgentSettings(), task_log_path: Callable[[str, str], Path] | None = None) -> None:
        self.executor = executor
        self.settings = settings
        self.task_log_path = task_log_path or (lambda task_id, node: Path(f"{task_id}-{node}.log"))

    def execute(self, request: AgentRequest) -> PhaseResult:
        log_path = self.task_log_path(request.work.task_id, request.node)
        model_cfg = self.settings.model
        model = model_cfg.name if model_cfg else None
        variant = model_cfg.variant if model_cfg else None
        provider_type = getattr(self.executor, "provider_type", None) or "executor"
        print(
            f"[{_now()}] {request.node}: starting {provider_type} "
            f"(agent={request.agent}, model={model or 'default'}, "
            f"variant={variant or '-'}, log={log_path})",
            flush=True,
        )
        previous_context = request.context
        execution_request = ExecutionRequest(
            task_id=request.work.task_id,
            workspace=request.workspace,
            prompt=request.prompt,
            agent=request.agent,
            model=model,
            variant=variant,
            context=previous_context,
            log_file=str(log_path),
        )
        try:
            result = self.executor.execute(execution_request)
        except ExecutorError as exc:
            raise AgentExecutionError(str(exc), context=previous_context) from exc
        except Exception as exc:
            raise AgentExecutionError(str(exc), context=previous_context) from exc
        result_context = result.context
        context = previous_context.merged(result_context)
        print(
            f"[{_now()}] {request.node}: finished in {result.duration_seconds:.0f}s "
            f"(exit={result.exit_code}, model={model or 'default'}, variant={variant or '-'})",
            flush=True,
        )
        if not result.success or result.exit_code != 0:
            error = (
                f"{request.node} executor ({provider_type}) reported failure"
                if result.exit_code == 0
                else f"{provider_type} ({request.agent}) exited with {result.exit_code}"
            )
            diagnostic = _failure_diagnostic(result.stdout, result.stderr)
            error = f"{error}; log={log_path}"
            if diagnostic:
                error = f"{error}; diagnostic={diagnostic}"
            raise AgentExecutionError(error, context=context)
        return PhaseResult(result, context)
