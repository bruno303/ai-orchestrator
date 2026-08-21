"""Issue-phase agent execution policy."""

from __future__ import annotations

from datetime import datetime

from orchestrator import config, workspace
from orchestrator.domain import Context
from orchestrator.providers import ExecutorError, ExecutionRequest
from orchestrator.runtime.errors import AgentExecutionError
from orchestrator.runtime.models import AgentRequest, PhaseResult


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class IssueAgentRunner:
    """Run issue phases with the configured primary/fallback policy."""

    def __init__(self, executor) -> None:
        self.executor = executor

    def execute(self, request: AgentRequest) -> PhaseResult:
        log_path = workspace.task_log_path(request.work.task_id, request.node)
        attempt = 1
        max_attempts = config.PHASE_MAX_ATTEMPTS if config.MODEL_FALLBACK_ENABLED else 1
        while attempt <= max_attempts:
            model_cfg = config.MODEL_PRIMARY if attempt == 1 else config.MODEL_FALLBACK
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
                provider_state={
                    key: value
                    for namespace in previous_context.values()
                    for key, value in namespace.items()
                },
                context=previous_context.merge_namespace("opencode", {
                    "log_file": str(log_path),
                    "detect_degenerate": config.MODEL_FALLBACK_ENABLED,
                }),
            )
            try:
                result = self.executor.execute(execution_request)
            except ExecutorError as exc:
                if not exc.retryable:
                    raise AgentExecutionError(str(exc), provider_state=previous_context.to_dict(), attempts=attempt) from exc
                attempt += 1
                if attempt > max_attempts:
                    raise AgentExecutionError(
                        f"{request.node} produced degenerate output after {max_attempts} attempts",
                        provider_state=previous_context.to_dict(),
                        attempts=max_attempts,
                    ) from exc
                print(
                    f"[{_now()}] {request.node}: degenerate output, retrying with "
                    f"model={(config.MODEL_FALLBACK.name if config.MODEL_FALLBACK else 'default')}",
                    flush=True,
                )
                continue
            except Exception as exc:
                raise AgentExecutionError(str(exc), provider_state=previous_context.to_dict(), attempts=attempt) from exc
            result_context = result.context
            if not result_context and result.provider_state:
                result_context = Context({"opencode": result.provider_state})
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
                raise AgentExecutionError(error, provider_state=context.to_dict(), attempts=attempt)
            return PhaseResult(result, attempt, context)
        raise AgentExecutionError(
            f"{request.node} produced degenerate output after {max_attempts} attempts",
            attempts=max_attempts,
        )
