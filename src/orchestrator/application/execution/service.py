"""Reusable provider-neutral operations for work-item execution."""

from __future__ import annotations

import re
from pathlib import Path

from orchestrator.domain import ChangeRequest, Context, PublishedChange
from orchestrator.application.ports import Destination, Executor, WorkspaceManager, WorkspaceRequest, WorkspaceResult
from orchestrator.application.execution.agent import AgentSettings, IssueAgentRunner
from orchestrator.application.execution.errors import (
    CleanupError,
    PlanValidationError,
    PublicationError,
    WorkspacePreparationError,
)
from orchestrator.application.execution.models import (
    AgentRequest,
    CleanupRequest,
    CleanupResult,
    ImplementationRequest,
    ImplementationResult,
    PhaseResult,
    PlanRequest,
    PlanResult,
    PrepareExecutionRequest,
    PrepareExecutionResult,
    PublishRequest,
    PublishResult,
    WorkContext,
)


# Generic application/runtime code may pass and merge Context, but may not
# inspect provider-specific namespaces or keys.
PLAN_FILE = ".agents/plans/plan.md"


def _clean_terminal_output(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")


def _extra_context(work: WorkContext) -> str:
    if not work.extra_context:
        return ""
    blocks = "\n\n".join(f"<comment>\n{text}\n</comment>" for text in work.extra_context)
    return f"\n\nAdditional requirements from comments:\n{blocks}"


def plan_prompt(work: WorkContext) -> str:
    return f"""You are planning work item {work.task_id} in repository {work.repository}.

Title: {work.title}

Description:
{work.body}
{_extra_context(work)}
Use the plan-implementation skill.

Analyze the request and repository, then produce a detailed implementation plan.
Do not modify files, run tests, push changes, or create a pull request.
"""


def implement_prompt(work: WorkContext, plan_path: str = PLAN_FILE) -> str:
    return f"""Implement work item {work.task_id} in repository {work.repository}.

Title: {work.title}

Description:
{work.body}
{_extra_context(work)}
The implementation plan is at {plan_path}. Execute it using the
/plan-implementation skill to implement this work item. Do not stop after
creating, revising, or saving a plan: modify the workspace to complete the
implementation. During implementation, run the repository's appropriate tests,
linters, and other relevant quality checks. Fix any failures and only finish
when the implementation and its validation are complete. Work only in this
workspace. Do not push or create a pull request.
"""


class ExecutionRuntime:
    """Perform execution steps without interpreting provider-owned context."""

    def __init__(self, executor: Executor, workspace_manager: WorkspaceManager, destination: Destination, *, repository_allowed=lambda _repository: True, agent_settings: AgentSettings = AgentSettings(), task_log_path=None) -> None:
        self.workspace_manager = workspace_manager
        self.destination = destination
        self.repository_allowed = repository_allowed
        self.agent = IssueAgentRunner(executor, agent_settings, task_log_path)

    def prepare(self, request: PrepareExecutionRequest) -> PrepareExecutionResult:
        if not self.repository_allowed(request.work.repository):
            raise WorkspacePreparationError(
                f"repository {request.work.repository} is not in the allowlist"
            )
        context = request.work.item.context.merged(request.context)
        try:
            result = self.workspace_manager.prepare(WorkspaceRequest(
                task_id=request.work.task_id,
                repository=request.work.repository,
                branch=request.branch,
                base_branch=request.base_branch,
                purpose="execution",
                target_ref=request.base_branch,
                checkout_mode="branch",
                workspace=request.workspace,
                context=context,
            ))
        except WorkspacePreparationError:
            raise
        except Exception as exc:
            raise WorkspacePreparationError(str(exc)) from exc
        result_context = result.context
        accumulated = context.merged(result_context)
        return PrepareExecutionResult(
            request.work,
            WorkspaceResult(
                result.workspace, result.branch, result_context, result.base_branch,
            ),
            result.base_branch or request.base_branch,
            accumulated,
        )

    def execute_phase(self, request: AgentRequest) -> PhaseResult:
        return self.agent.execute(request)

    def plan(self, request: PlanRequest) -> PlanResult:
        phase = self.agent.execute(AgentRequest(
            request.work, "plan", "plan", plan_prompt(request.work), request.workspace, request.context
        ))
        plan_path = Path(request.workspace) / PLAN_FILE
        try:
            if not plan_path.is_file() and phase.execution.stdout.strip():
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text(_clean_terminal_output(phase.execution.stdout).strip() + "\n")
            if not plan_path.is_file():
                raise PlanValidationError(
                    f"planning completed without creating {PLAN_FILE}",
                    context=phase.context,
                )
            if not plan_path.read_text().strip():
                raise PlanValidationError(
                    f"planning completed with an empty {PLAN_FILE}",
                    context=phase.context,
                )
        except PlanValidationError:
            raise
        except OSError as exc:
            raise PlanValidationError(
                str(exc), context=phase.context
            ) from exc
        return PlanResult(phase.execution.stdout[:4000], PLAN_FILE, phase)

    def implement(self, request: ImplementationRequest) -> ImplementationResult:
        phase = self.agent.execute(AgentRequest(
            request.work, "implement", "build", implement_prompt(request.work, request.plan_path),
            request.workspace, request.context,
        ))
        return ImplementationResult(phase.execution.stdout[:4000], phase)

    def publish(self, request: PublishRequest) -> PublishResult:
        context = request.work.item.context.merged(request.context)
        try:
            result = self.destination.publish(ChangeRequest(
                task_id=request.work.task_id,
                repository=request.work.repository,
                title=f"feat: {request.work.title}"[:72],
                description=request.work.body,
                source_ref=request.source_ref,
                target_ref=request.target_ref,
                context=context,
            ))
        except Exception as exc:
            raise PublicationError(str(exc)) from exc
        if not isinstance(result, PublishedChange):
            external_id = getattr(result, "external_id", None)
            result_context = context
            result = PublishedChange(
                external_id, getattr(result, "url", None),
                getattr(self.destination, "provider_type", ""),
                result_context,
            )
        return PublishResult(result)

    def cleanup(self, request: CleanupRequest) -> CleanupResult:
        try:
            self.workspace_manager.cleanup(request.workspace)
        except Exception as exc:
            raise CleanupError(str(exc)) from exc
        return CleanupResult(request.workspace.workspace)
