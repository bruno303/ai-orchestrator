"""Unit tests for reusable execution and review runtime facades."""

from __future__ import annotations

import pytest

from orchestrator import config, opencode
from orchestrator.domain import Context, PublishedChange, PublishedReview, ReviewOutcome, ReviewTarget, WorkItem
from orchestrator.providers import ExecutionResult, PublicationResult, ReviewEvent, ReviewResult, WorkspaceResult
from orchestrator.runtime import compose_execution_runtime
from orchestrator.runtime.errors import ReviewExecutionError
from orchestrator.runtime.models import (
    AgentRequest,
    CleanupRequest,
    CleanupReviewRequest,
    ExecuteReviewRequest,
    ImplementationRequest,
    IssueContext,
    PlanRequest,
    PrepareExecutionRequest,
    PrepareReviewRequest,
    PublishRequest,
    PublishReviewRequest,
    TestRequest as RuntimeTestRequest,
    WorkContext,
)
from orchestrator.runtime.review import REVIEW_PROMPT, ReviewRuntime


class ExecutionWorkspace:
    def __init__(self, path: str):
        self.path = path
        self.requests = []
        self.cleaned = []

    def prepare(self, request):
        self.requests.append(request)
        return WorkspaceResult(self.path, request.branch, {"base_branch": "main", "workspace_id": "w1"})

    def cleanup(self, result):
        self.cleaned.append(result)


class ExecutionAgent:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if request.agent == "plan":
            return ExecutionResult(True, 0, stdout="# Plan\n\nDo it.", provider_state={"session": "s1"})
        return ExecutionResult(True, 0, stdout=f"{request.agent} complete", provider_state={"session": "s1"})


def _context() -> IssueContext:
    return IssueContext("repo#7", "company/backend", 7, "Add feature", "Body")


def test_execution_runtime_exposes_independent_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_repository_allowed", lambda repository: True)
    workspace_manager = ExecutionWorkspace(str(tmp_path))
    executor = ExecutionAgent()
    runtime = compose_execution_runtime(
        executor=executor,
        workspace_manager=workspace_manager,
        destination=type("Destination", (), {"publish": lambda self, request: PublicationResult(number=17)})(),
    )
    prepared = runtime.prepare(PrepareExecutionRequest(_context(), workspace=str(tmp_path)))
    plan = runtime.plan(PlanRequest(_context(), prepared.workspace.workspace))
    implementation = runtime.implement(
        ImplementationRequest(_context(), prepared.workspace.workspace, provider_state=plan.phase.provider_state)
    )
    tests = runtime.test(RuntimeTestRequest(_context(), prepared.workspace.workspace, implementation.phase.provider_state))
    published = runtime.publish(
        PublishRequest(_context(), prepared.workspace.workspace, prepared.workspace.branch, prepared.base_branch)
    )
    runtime.cleanup(CleanupRequest("company/backend", prepared.workspace))

    assert plan.plan_path == ".agents/plans/plan.md"
    assert implementation.summary == "build complete"
    assert tests.summary == "build complete"
    assert published.publication.number == 17
    assert workspace_manager.requests[0].purpose == "execution"
    assert len(workspace_manager.cleaned) == 1
    assert executor.requests[1].provider_state["session"] == "s1"


def test_execution_runtime_retries_degenerate_phase_with_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODEL_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "PHASE_MAX_ATTEMPTS", 2)

    class FlakyExecutor:
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            if self.calls == 1:
                raise opencode.DegenerateOutputError("loop")
            return ExecutionResult(True, 0, stdout="ok")

    runtime = compose_execution_runtime(
        executor=FlakyExecutor(), workspace_manager=ExecutionWorkspace(str(tmp_path)), destination=object()
    )
    result = runtime.execute_phase(AgentRequest(_context(), "implement", "build", "prompt", str(tmp_path)))
    assert result.attempts == 2


def test_execution_runtime_uses_requested_plan_path(tmp_path):
    executor = ExecutionAgent()
    runtime = compose_execution_runtime(
        executor=executor,
        workspace_manager=ExecutionWorkspace(str(tmp_path)),
        destination=object(),
    )
    requested = ".agents/plans/custom.md"
    runtime.implement(ImplementationRequest(_context(), str(tmp_path), plan_path=requested))
    assert requested in executor.requests[0].prompt


def test_cleanup_preserves_custom_workspace_provider_state(tmp_path):
    original_state = {"workspace_id": "w1"}
    captured = {}

    class CustomWorkspace:
        provider_type = "custom_workspace"

        def cleanup(self, result):
            captured["provider_state"] = result.provider_state

    runtime = compose_execution_runtime(
        executor=ExecutionAgent(), workspace_manager=CustomWorkspace(), destination=object()
    )
    runtime.cleanup(
        CleanupRequest(
            "company/backend",
            WorkspaceResult(str(tmp_path), "branch", original_state),
        )
    )

    assert captured["provider_state"] is original_state
    assert captured["provider_state"] == {"workspace_id": "w1"}


def test_review_runtime_keeps_review_workspace_mode_explicit(tmp_path):
    captured = {}

    class ReviewWorkspace:
        def prepare(self, request):
            captured["purpose"] = request.purpose
            return WorkspaceResult(str(tmp_path), "", {"repo_dir": str(tmp_path)})

        def cleanup(self, result):
            captured["cleaned"] = True

    class ReviewExecutor:
        def execute(self, request):
            captured["request"] = request
            return ReviewResult(True, verdict="approve", summary="ok")

    class ReviewDestination:
        def publish(self, request, result):
            captured["published"] = True

    event = ReviewEvent("event-1", "company/backend", provider_state={"number": 3, "head_sha": "sha"})
    runtime = ReviewRuntime(ReviewExecutor(), ReviewWorkspace(), ReviewDestination())
    prepared = runtime.prepare(PrepareReviewRequest(event, "review:company/backend#3"))
    execution = runtime.execute_review(ExecuteReviewRequest(prepared, REVIEW_PROMPT))
    runtime.publish_review(PublishReviewRequest(prepared, execution))
    runtime.cleanup_review(CleanupReviewRequest(prepared))

    assert captured["purpose"] == "review"
    assert captured["request"].provider_state["log_file"].endswith("review.log")
    assert captured["published"] and captured["cleaned"]


def test_review_prompt_requires_code_review_skill():
    assert "/code-review" in REVIEW_PROMPT
    assert "ONLY valid JSON" in REVIEW_PROMPT


def test_review_runtime_turns_executor_failure_into_typed_error(tmp_path):
    event = ReviewEvent("event-1", "company/backend", provider_state={"number": 3})

    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(str(tmp_path), "", {})

        def cleanup(self, result):
            pass

    class Executor:
        def execute(self, request):
            return ReviewResult(False, summary="invalid structured review output")

    runtime = ReviewRuntime(Executor(), Workspace(), object())
    prepared = runtime.prepare(PrepareReviewRequest(event, "review:company/backend#3"))
    with pytest.raises(ReviewExecutionError, match="invalid structured"):
        runtime.execute_review(ExecuteReviewRequest(prepared, REVIEW_PROMPT))


def test_provider_neutral_execution_preserves_all_context_namespaces(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_repository_allowed", lambda repository: True)
    captured = {}
    work = WorkContext(WorkItem(
        "ABC-42", "company/backend", "Add feature", "Details",
        context=Context({
            "fake_source": {"ticket": "ABC-42"},
            "git": {"repository_url": "fake://repository", "branch": "task-ABC-42"},
        }),
    ))

    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(
                str(tmp_path), request.branch, context=request.context.merge_namespace(
                    "git", {"workspace": str(tmp_path), "base_branch": "main"}
                ), base_branch="main",
            )

        def cleanup(self, result):
            pass

    class Executor:
        def execute(self, request):
            stdout = "# Plan\n\nDo it." if request.agent == "plan" else "complete"
            return ExecutionResult(
                True, 0, stdout=stdout,
                context=request.context.merge_namespace("opencode", {"session_id": "session-1"}),
            )

    class Destination:
        def publish(self, request):
            captured["request"] = request
            return PublishedChange("change-42", provider="fake", context=request.context)

    runtime = compose_execution_runtime(
        executor=Executor(), workspace_manager=Workspace(), destination=Destination()
    )
    prepared = runtime.prepare(PrepareExecutionRequest(work))
    planned = runtime.plan(PlanRequest(work, prepared.workspace.workspace, prepared.context))
    implemented = runtime.implement(ImplementationRequest(
        work, prepared.workspace.workspace, context=planned.phase.context
    ))
    tested = runtime.test(RuntimeTestRequest(
        work, prepared.workspace.workspace, implemented.phase.context
    ))
    published = runtime.publish(PublishRequest(
        work, prepared.workspace.workspace, "task-ABC-42", "main", tested.phase.context
    ))

    assert published.publication.id == "change-42"
    assert set(captured["request"].context) == {"fake_source", "git", "opencode"}
    assert captured["request"].task_id == "ABC-42"


def test_provider_neutral_review_uses_opaque_id_without_github_context(tmp_path):
    captured = {}
    target = ReviewTarget(
        "MR-abc", "company/backend", source_ref="feature", target_ref="main", revision="rev",
        context=Context({"fake_review": {"merge_request": "abc"}, "git": {"repository_url": "fake"}}),
    )

    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(str(tmp_path), "", context=request.context)

        def cleanup(self, result):
            captured["cleaned"] = True

    class Executor:
        def execute(self, request):
            assert request.task_id == "MR-abc"
            return ReviewOutcome(True, "approve", "ok", context=request.context)

    class Destination:
        def publish(self, current_target, outcome):
            captured["target"] = current_target
            return PublishedReview("published-1", provider="fake", context=current_target.context)

    runtime = ReviewRuntime(Executor(), Workspace(), Destination())
    prepared = runtime.prepare(PrepareReviewRequest(target))
    execution = runtime.execute_review(ExecuteReviewRequest(prepared, REVIEW_PROMPT))
    published = runtime.publish_review(PublishReviewRequest(prepared, execution))
    runtime.cleanup_review(CleanupReviewRequest(prepared))

    assert published.publication.id == "published-1"
    assert captured["target"].id == "MR-abc"
    assert "github" not in captured["target"].context
    assert captured["cleaned"]
