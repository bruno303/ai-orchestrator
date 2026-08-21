"""Reusable provider-neutral operations for work-item execution."""

from __future__ import annotations

import re
from pathlib import Path

from orchestrator import config
from orchestrator.domain import ChangeRequest, Context, PublishedChange
from orchestrator.providers import Destination, Executor, WorkspaceManager, WorkspaceRequest, WorkspaceResult
from orchestrator.runtime.agent import IssueAgentRunner
from orchestrator.runtime.errors import (
    AgentExecutionError,
    CleanupError,
    PlanValidationError,
    PublicationError,
    QualityGateError,
    WorkspacePreparationError,
)
from orchestrator.runtime.models import (
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
    TestRequest,
    TestResult,
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
subagent-plan-execution skill. Work only in this workspace. Do not push or
create a pull request.
"""


def test_prompt(work: WorkContext) -> str:
    return f"""Run the appropriate test suite for repository {work.repository}, work item {work.task_id}.
Do not modify code. Report all results and failures.
"""


class ExecutionRuntime:
    """Perform execution steps without interpreting provider-owned context."""

    def __init__(self, executor: Executor, workspace_manager: WorkspaceManager, destination: Destination) -> None:
        self.workspace_manager = workspace_manager
        self.destination = destination
        self.agent = IssueAgentRunner(executor)

    def prepare(self, request: PrepareExecutionRequest) -> PrepareExecutionResult:
        if not config.is_repository_allowed(request.work.repository):
            raise WorkspacePreparationError(
                f"repository {request.work.repository} is not in the allowlist"
            )
        context = request.work.item.context.merged(request.context)
        legacy_state = {
            key: value for namespace in context.values() for key, value in namespace.items()
        }
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
                provider_state=legacy_state,
            ))
        except WorkspacePreparationError:
            raise
        except Exception as exc:
            raise WorkspacePreparationError(str(exc)) from exc
        result_context = result.context
        if not result_context and result.provider_state:
            result_context = Context({"workspace": result.provider_state})
        accumulated = context.merged(result_context)
        return PrepareExecutionResult(
            request.work,
            WorkspaceResult(
                result.workspace, result.branch, result.provider_state,
                result_context, result.base_branch,
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
                    provider_state=phase.context.to_dict(), attempts=phase.attempts,
                )
            if not plan_path.read_text().strip():
                raise PlanValidationError(
                    f"planning completed with an empty {PLAN_FILE}",
                    provider_state=phase.context.to_dict(), attempts=phase.attempts,
                )
        except PlanValidationError:
            raise
        except OSError as exc:
            raise PlanValidationError(
                str(exc), provider_state=phase.context.to_dict(), attempts=phase.attempts
            ) from exc
        return PlanResult(phase.execution.stdout[:4000], PLAN_FILE, phase)

    def implement(self, request: ImplementationRequest) -> ImplementationResult:
        phase = self.agent.execute(AgentRequest(
            request.work, "implement", "build", implement_prompt(request.work, request.plan_path),
            request.workspace, request.context,
        ))
        return ImplementationResult(phase.execution.stdout[:4000], phase)

    def test(self, request: TestRequest) -> TestResult:
        try:
            phase = self.agent.execute(AgentRequest(
                request.work, "test", "build", test_prompt(request.work), request.workspace, request.context
            ))
        except AgentExecutionError as exc:
            raise QualityGateError(
                str(exc), provider_state=exc.provider_state, attempts=exc.attempts
            ) from exc
        return TestResult(phase.execution.stdout[:4000], phase)

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
            if getattr(result, "provider_state", None):
                result_context = result_context.merge_namespace(
                    "destination", getattr(result, "provider_state")
                )
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
