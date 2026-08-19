"""Reusable runtime operations for the issue-to-PR workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orchestrator import config, workspace
from orchestrator.providers import (
    Destination,
    Executor,
    PublicationRequest,
    WorkspaceManager,
    WorkspaceRequest,
    WorkspaceResult,
    validate_provider_state,
)
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
    IssueContext,
    PhaseResult,
    PlanRequest,
    PlanResult,
    PrepareExecutionRequest,
    PrepareExecutionResult,
    PublishRequest,
    PublishResult,
    TestRequest,
    TestResult,
)


PLAN_FILE = ".agents/plans/plan.md"


def _clean_terminal_output(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")


def _extra_context(context: IssueContext) -> str:
    if not context.extra_context:
        return ""
    blocks = "\n\n".join(f"<comment>\n{text}\n</comment>" for text in context.extra_context)
    return f"\n\nAdditional requirements from comments:\n{blocks}"


def plan_prompt(context: IssueContext) -> str:
    return f"""You are planning the implementation of GitHub issue #{context.issue_number} in repository {context.repository}.

Issue title: {context.title}

Issue body:
{context.body}
{_extra_context(context)}
Use the plan-implementation skill.

Requirements:
1. Analyze the issue and the repository.
2. Produce the implementation plan in your final response. The orchestrator
   will save your response to {PLAN_FILE} in this workspace. The plan must use
   the format required by the subagent-plan-execution skill:
   clearly separated tasks, each with **Files:** and **Dependencies:**, detailed enough for an implementer unfamiliar with the project.
3. The plan must cover: requirements, implementation steps, files likely to change, tests required, potential risks, and open questions if the requirements are ambiguous.

Restrictions:
- Do NOT modify or create repository files. Planning only.
- Do NOT create a pull request.
- Do NOT run tests or builds.
"""


def implement_prompt(context: IssueContext, plan_path: str = PLAN_FILE) -> str:
    return f"""You are implementing GitHub issue #{context.issue_number} in repository {context.repository}.

Issue title: {context.title}

Issue body:
{context.body}
{_extra_context(context)}
A plan has been written to {plan_path}.

Execute the plan using the subagent-plan-execution skill. Invoke it explicitly: load the skill "subagent-plan-execution" (base directory: {config.SKILL_SUBAGENT_PLAN_EXECUTION}) and follow its steps exactly:
- Step 0: read {plan_path}
    - Step 1: for each task, write the brief file, dispatch a fresh implementer subagent, then dispatch a fresh reviewer subagent
- Step 2: run the quality gate (build, tests, lint) and fix any failures

The skill may perform implementation and reviewer subagent passes internally.
The orchestrator has no separate top-level review phase; after implementation,
the workflow runs the standalone test phase and then publishes the PR.

Context:
- Work only inside this workspace (the task worktree).
- Do NOT create a pull request or push anything.
- Report the final result when done.
"""


def test_prompt(context: IssueContext) -> str:
    return f"""Run the test suite of the project in this workspace (repository {context.repository}, issue #{context.issue_number}).

Determine the appropriate test command (e.g. pytest, npm test, ./gradlew test) and run it.
Do NOT modify any code.
Report the results, including any failures.
"""


def pr_body(context: IssueContext, current_body: str | None = None) -> str:
    closes = f"Closes #{context.issue_number}"
    if not current_body:
        return closes
    lines = current_body.splitlines()
    remainder_lines: list[str] = []
    skip_blank = False
    for line in lines:
        if line.strip() == closes:
            skip_blank = True
            continue
        if skip_blank and line == "":
            skip_blank = False
            continue
        skip_blank = False
        remainder_lines.append(line)
    remainder = "\n".join(remainder_lines).strip("\n")
    return f"{closes}\n\n{remainder}" if remainder else closes


class ExecutionRuntime:
    """Perform issue workflow steps without depending on LangGraph state."""

    def __init__(self, executor: Executor, workspace_manager: WorkspaceManager, destination: Destination) -> None:
        self.workspace_manager = workspace_manager
        self.destination = destination
        self.agent = IssueAgentRunner(executor)

    def prepare(self, request: PrepareExecutionRequest) -> PrepareExecutionResult:
        if not config.is_repository_allowed(request.context.repository):
            raise WorkspacePreparationError(
                f"repository {request.context.repository} is not in the allowlist"
            )
        current_state = validate_provider_state(request.provider_state)
        branch = request.branch or f"ai/issue-{request.context.issue_number}"
        workspace_path = request.workspace or str(
            workspace.task_workspace(request.context.repository, request.context.issue_number)
        )
        try:
            result = self.workspace_manager.prepare(
                WorkspaceRequest(
                    task_id=request.context.task_id,
                    repository=request.context.repository,
                    branch=branch,
                    base_branch=request.base_branch,
                    provider_state={
                        **current_state,
                        "repository_url": current_state.get("repository_url") or request.context.provider_state.get("repository_url"),
                        "workspace": workspace_path,
                        "issue_number": request.context.issue_number,
                    },
                    purpose="execution",
                )
            )
            provider_state = validate_provider_state(result.provider_state)
        except WorkspacePreparationError:
            raise
        except Exception as exc:
            raise WorkspacePreparationError(str(exc)) from exc
        base_branch = provider_state.get("base_branch") or request.base_branch
        return PrepareExecutionResult(
            request.context,
            WorkspaceResult(result.workspace, result.branch, provider_state),
            base_branch,
        )

    def execute_phase(self, request: AgentRequest) -> PhaseResult:
        return self.agent.execute(request)

    def plan(self, request: PlanRequest) -> PlanResult:
        phase = self.agent.execute(
            AgentRequest(request.context, "plan", "plan", plan_prompt(request.context), request.workspace, request.provider_state)
        )
        plan_path = Path(request.workspace) / PLAN_FILE
        try:
            if not plan_path.is_file() and phase.execution.stdout.strip():
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text(_clean_terminal_output(phase.execution.stdout).strip() + "\n")
            if not plan_path.is_file():
                raise PlanValidationError(f"planning completed without creating {PLAN_FILE}", provider_state=phase.provider_state, attempts=phase.attempts)
            if not plan_path.read_text().strip():
                raise PlanValidationError(f"planning completed with an empty {PLAN_FILE}", provider_state=phase.provider_state, attempts=phase.attempts)
        except PlanValidationError:
            raise
        except OSError as exc:
            raise PlanValidationError(str(exc), provider_state=phase.provider_state, attempts=phase.attempts) from exc
        return PlanResult(phase.execution.stdout[:4000], PLAN_FILE, phase)

    def implement(self, request: ImplementationRequest) -> ImplementationResult:
        phase = self.agent.execute(
                AgentRequest(request.context, "implement", "build", implement_prompt(request.context, request.plan_path), request.workspace, request.provider_state)
        )
        return ImplementationResult(phase.execution.stdout[:4000], phase)

    def test(self, request: TestRequest) -> TestResult:
        try:
            phase = self.agent.execute(
                AgentRequest(request.context, "test", "build", test_prompt(request.context), request.workspace, request.provider_state)
            )
        except AgentExecutionError as exc:
            raise QualityGateError(
                str(exc), provider_state=exc.provider_state, attempts=exc.attempts
            ) from exc
        return TestResult(phase.execution.stdout[:4000], phase)

    def publish(self, request: PublishRequest) -> PublishResult:
        title = f"feat: {request.context.title}"[:72]
        try:
            result = self.destination.publish(
                PublicationRequest(
                    repository=request.context.repository,
                    title=title,
                    body=pr_body(request.context),
                    head=request.head,
                    base=request.base,
                    provider_state={
                        "workspace": request.workspace,
                        "issue_number": request.context.issue_number,
                        **request.provider_state,
                    },
                )
            )
        except Exception as exc:
            raise PublicationError(str(exc)) from exc
        try:
            validate_provider_state(result.provider_state)
        except TypeError as exc:
            raise PublicationError(str(exc)) from exc
        return PublishResult(result)

    def cleanup(self, request: CleanupRequest) -> CleanupResult:
        provider_state = request.workspace.provider_state
        if getattr(self.workspace_manager, "provider_type", "") == "git" and "repository" not in provider_state:
            provider_state = {**provider_state, "repository": request.repository}
        try:
            self.workspace_manager.cleanup(
                WorkspaceResult(request.workspace.workspace, request.workspace.branch, provider_state)
            )
        except Exception as exc:
            raise CleanupError(str(exc)) from exc
        return CleanupResult(request.workspace.workspace)
