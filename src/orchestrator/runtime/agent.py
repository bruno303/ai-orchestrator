"""Issue-phase agent execution policy."""

from __future__ import annotations

from datetime import datetime

from orchestrator import config, opencode, workspace
from orchestrator.providers import ExecutionRequest, validate_provider_state
from orchestrator.runtime.errors import AgentExecutionError
from orchestrator.runtime.models import AgentRequest, PhaseResult


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class IssueAgentRunner:
    """Run issue phases with the configured primary/fallback policy."""

    def __init__(self, executor) -> None:
        self.executor = executor

    def execute(self, request: AgentRequest) -> PhaseResult:
        log_path = workspace.task_log_path(request.context.task_id, request.node)
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
                previous_state = validate_provider_state(request.provider_state)
            except TypeError as exc:
                raise AgentExecutionError(str(exc), attempts=attempt) from exc
            execution_request = ExecutionRequest(
                task_id=request.context.task_id,
                workspace=request.workspace,
                prompt=request.prompt,
                agent=request.agent,
                model=model,
                variant=variant,
                provider_state={
                    **previous_state,
                    "log_file": str(log_path),
                    "detect_degenerate": config.MODEL_FALLBACK_ENABLED,
                },
            )
            try:
                result = self.executor.execute(execution_request)
            except opencode.DegenerateOutputError as exc:
                attempt += 1
                if attempt > max_attempts:
                    raise AgentExecutionError(
                        f"{request.node} produced degenerate output after {max_attempts} attempts",
                        provider_state=previous_state,
                        attempts=max_attempts,
                    ) from exc
                print(
                    f"[{_now()}] {request.node}: degenerate output, retrying with "
                    f"model={(config.MODEL_FALLBACK.name if config.MODEL_FALLBACK else 'default')}",
                    flush=True,
                )
                continue
            except opencode.OpenCodeError as exc:
                raise AgentExecutionError(str(exc), provider_state=previous_state, attempts=attempt) from exc
            except Exception as exc:
                raise AgentExecutionError(str(exc), provider_state=previous_state, attempts=attempt) from exc
            try:
                provider_state = validate_provider_state({**previous_state, **result.provider_state})
            except TypeError as exc:
                raise AgentExecutionError(str(exc), provider_state=previous_state, attempts=attempt) from exc
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
                raise AgentExecutionError(error, provider_state=provider_state, attempts=attempt)
            return PhaseResult(result, attempt, provider_state)
        raise AgentExecutionError(
            f"{request.node} produced degenerate output after {max_attempts} attempts",
            attempts=max_attempts,
        )
