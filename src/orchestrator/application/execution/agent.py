"""Issue-phase agent execution policy."""

from __future__ import annotations

from datetime import datetime

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.application.ports import ExecutorError, ExecutionRequest
from orchestrator.application.execution.errors import AgentExecutionError
from orchestrator.application.execution.models import AgentRequest, PhaseResult


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass(frozen=True)
class AgentSettings:
    primary_model: object | None = None
    fallback_model: object | None = None
    fallback_enabled: bool = False
    max_attempts: int = 1


class IssueAgentRunner:
    """Run issue phases with the configured primary/fallback policy."""

    def __init__(self, executor, settings: AgentSettings = AgentSettings(), task_log_path: Callable[[str, str], Path] | None = None) -> None:
        self.executor = executor
        self.settings = settings
        self.task_log_path = task_log_path or (lambda task_id, node: Path(f"{task_id}-{node}.log"))

    def execute(self, request: AgentRequest) -> PhaseResult:
        log_path = self.task_log_path(request.work.task_id, request.node)
        attempt = 1
        max_attempts = self.settings.max_attempts if self.settings.fallback_enabled else 1
        while attempt <= max_attempts:
            model_cfg = self.settings.primary_model if attempt == 1 else self.settings.fallback_model
            model = model_cfg.name if model_cfg else None
            variant = model_cfg.variant if model_cfg else None
            print(
                f"[{_now()}] {request.node}: starting opencode "
                f"(agent={request.agent}, attempt={attempt}, model={model or 'default'}, "
                f"variant={variant or '-'}, log={log_path})",
                flush=True,
            )
            try:
                previous_context = request.context
            except TypeError as exc:
                raise AgentExecutionError(str(exc), attempts=attempt) from exc
            execution_request = ExecutionRequest(
                task_id=request.work.task_id,
                workspace=request.workspace,
                prompt=request.prompt,
                agent=request.agent,
                model=model,
                variant=variant,
                context=previous_context.merge_namespace("opencode", {
                    "log_file": str(log_path),
                    "detect_degenerate": self.settings.fallback_enabled,
                }),
            )
            try:
                result = self.executor.execute(execution_request)
            except ExecutorError as exc:
                if not exc.retryable:
                    raise AgentExecutionError(str(exc), context=previous_context, attempts=attempt) from exc
                attempt += 1
                if attempt > max_attempts:
                    raise AgentExecutionError(
                        f"{request.node} produced degenerate output after {max_attempts} attempts",
                        context=previous_context,
                        attempts=max_attempts,
                    ) from exc
                print(
                    f"[{_now()}] {request.node}: degenerate output, retrying with "
                    f"model={(self.settings.fallback_model.name if self.settings.fallback_model else 'default')}",
                    flush=True,
                )
                continue
            except Exception as exc:
                raise AgentExecutionError(str(exc), context=previous_context, attempts=attempt) from exc
            result_context = result.context
            context = previous_context.merged(result_context)
            print(
                f"[{_now()}] {request.node}: finished in {result.duration_seconds:.0f}s "
                f"(exit={result.exit_code}, attempt={attempt}, model={model or 'default'}, "
                f"variant={variant or '-'})",
                flush=True,
            )
            if not result.success or result.exit_code != 0:
                error = (
                    f"{request.node} executor ({request.agent}) reported failure"
                    if result.exit_code == 0
                    else f"opencode ({request.agent}) exited with {result.exit_code}"
                )
                raise AgentExecutionError(error, context=context, attempts=attempt)
            return PhaseResult(result, attempt, context)
        raise AgentExecutionError(
            f"{request.node} produced degenerate output after {max_attempts} attempts",
            attempts=max_attempts,
        )
