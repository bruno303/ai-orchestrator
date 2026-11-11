"""Per-stage workflow catalogs and graph integration for phases 2-4."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from orchestrator.application.discussion import PreparedDiscussion
from orchestrator.application.execution.models import (
    ExecuteReviewRequest,
    ExecuteReviewResult,
    PrepareReviewResult,
    PublishReviewResult,
)
from orchestrator.application.ports import (
    DiscussionResult,
    ExecutionResult,
    WorkspaceResult,
)
from orchestrator.application.review import ReviewApplication
from orchestrator.application.triage import TriageApplication
from orchestrator.application.workflow import StepSpec, default_steps
from orchestrator.domain import (
    Context,
    PublishedReview,
    PublishedTriage,
    ReviewOutcome,
    ReviewTarget,
    TriageOutcome,
    TriageTarget,
)
from orchestrator.infra.langgraph import discussion_steps, review_steps, triage_steps
from orchestrator.infra.langgraph.builder import build_workflow
from orchestrator.infra.langgraph.state import (
    COMPLETED,
    FAILED,
    DiscussionState,
    ReviewState,
    TriageState,
)

# Prepared workspaces for workspace-consuming steps: the catalogs guard on a
# real existing path rather than allowing Path("") to resolve to the repo cwd.
_WORKSPACE = tempfile.mkdtemp(prefix="orchestrator-stage-tests-")


# --- review -----------------------------------------------------------------


class FakeReviewRuntime:
    def __init__(self, *, fail_review: bool = False):
        self.calls: list[str] = []
        self.requests: list[ExecuteReviewRequest] = []
        self.fail_review = fail_review

    def prepare(self, request):
        self.calls.append("prepare")
        return PrepareReviewResult(
            request.target,
            WorkspaceResult(_WORKSPACE, "feature", request.target.context, "main"),
            request.target.context,
        )

    def execute_review(self, request):
        self.calls.append("review")
        self.requests.append(request)
        if self.fail_review:
            raise RuntimeError("review executor failed")
        return ExecuteReviewResult(
            SimpleNamespace(), ReviewOutcome(True, "approve", "ok", context=request.prepared.context)
        )

    def publish_review(self, request):
        self.calls.append("publish")
        return PublishReviewResult(
            PublishedReview("9", provider="fake", context=request.prepared.context)
        )

    def cleanup_review(self, request):
        self.calls.append("cleanup")
        return SimpleNamespace(workspace=request.prepared.workspace.workspace)

    def task_log_path(self, task_id, node):
        return Path(_WORKSPACE) / f"{task_id}-{node}.log"


def _review_target() -> ReviewTarget:
    return ReviewTarget(
        "review:company/backend#4", "company/backend", source_ref="feature",
        target_ref="main", revision="abc",
        context=Context({"github": {"pr_number": 4}}),
    )


def _review_graph(runtime, steps=None, deps=None):
    deps = deps or review_steps.ReviewStageDeps(runtime=runtime)
    return build_workflow(
        list(steps or default_steps("review")), review_steps.STEP_FACTORIES, deps,
        state_schema=ReviewState,
    )


def test_default_review_graph_runs_checkout_review_publish():
    runtime = FakeReviewRuntime()
    graph = _review_graph(runtime)

    result = graph.invoke(review_steps.seed(_review_target()))

    assert runtime.calls == ["prepare", "review", "publish"]
    assert result["status"] == COMPLETED
    assert result["output"]["external_id"] == "9"
    assert sorted(result["step_results"]) == ["checkout", "publish", "review"]


def test_review_graph_aborts_publication_when_the_review_step_fails():
    runtime = FakeReviewRuntime(fail_review=True)
    graph = _review_graph(runtime)

    result = graph.invoke(review_steps.seed(_review_target()))

    assert runtime.calls == ["prepare", "review"]
    assert result["status"] == FAILED
    assert "review executor failed" in result["error"]
    entry = result["step_results"]["review"]
    assert entry["status"] == "failed"
    assert "review executor failed" in entry["error"]


def test_review_step_uses_the_configured_prompt_file():
    runtime = FakeReviewRuntime()
    deps = review_steps.ReviewStageDeps(
        runtime=runtime, prompts={"review": "custom review prompt"},
    )
    graph = _review_graph(runtime, deps=deps)

    graph.invoke(review_steps.seed(_review_target()))

    assert runtime.requests[0].prompt == "custom review prompt"


def test_renamed_review_step_uses_its_name_for_the_executor_log():
    runtime = FakeReviewRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("audit", "review"),
        StepSpec("publish", "publish"),
    ]
    deps = review_steps.ReviewStageDeps(runtime=runtime)

    result = _review_graph(runtime, steps=steps, deps=deps).invoke(
        review_steps.seed(_review_target())
    )

    assert result["status"] == COMPLETED
    assert runtime.requests[0].node == "audit"


def test_review_step_routes_to_the_override_runtime():
    default_runtime = FakeReviewRuntime()
    override_runtime = FakeReviewRuntime()
    deps = review_steps.ReviewStageDeps(
        runtime=default_runtime, step_runtimes={"review": override_runtime},
    )
    graph = _review_graph(default_runtime, deps=deps)

    graph.invoke(review_steps.seed(_review_target()))

    assert default_runtime.calls == ["prepare", "publish"]
    assert override_runtime.calls == ["review"]


def test_review_command_step_aborts_the_chain_on_failure():
    runtime = FakeReviewRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("lint", "command", command="false"),
    ]
    deps = review_steps.ReviewStageDeps(
        runtime=runtime,
        command_runner=lambda workspace, command, timeout, log_file: ExecutionResult(
            False, 2, stderr="boom"
        ),
    )

    result = _review_graph(runtime, steps=steps, deps=deps).invoke(
        review_steps.seed(_review_target())
    )

    assert result["status"] == FAILED
    assert "command step 'lint' failed (exit 2)" in result["step_results"]["lint"]["error"]


def test_review_command_failure_with_continue_keeps_the_prior_completion():
    runtime = FakeReviewRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("review", "review"),
        StepSpec("publish", "publish"),
        StepSpec("lint", "command", command="false", on_failure="continue"),
    ]
    deps = review_steps.ReviewStageDeps(
        runtime=runtime,
        command_runner=lambda workspace, command, timeout, log_file: ExecutionResult(
            False, 2, stderr="boom"
        ),
    )

    result = _review_graph(runtime, steps=steps, deps=deps).invoke(
        review_steps.seed(_review_target())
    )

    assert result["status"] == COMPLETED
    assert result["step_results"]["lint"]["status"] == "failed"
    assert "exit 2" in result["step_results"]["lint"]["error"]


def test_review_command_step_records_success_output():
    runtime = FakeReviewRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("lint", "command", command="echo ok"),
    ]
    deps = review_steps.ReviewStageDeps(
        runtime=runtime,
        command_runner=lambda workspace, command, timeout, log_file: ExecutionResult(
            True, 0, stdout="clean"
        ),
    )

    result = _review_graph(runtime, steps=steps, deps=deps).invoke(
        review_steps.seed(_review_target())
    )

    assert result["status"] == "PREPARING"  # no publication step configured
    assert result["step_results"]["lint"]["status"] == "succeeded"
    assert result["step_results"]["lint"]["summary"] == "clean"


def test_review_application_runs_the_graph_and_keeps_loop_owned_cleanup():
    runtime = FakeReviewRuntime()
    graph = _review_graph(runtime)
    target = _review_target()
    input_source = SimpleNamespace(poll=lambda: [target])
    app = ReviewApplication(
        input_source,
        runtime=runtime,
        run_workflow=lambda current: graph.invoke(review_steps.seed(current)),
    )

    assert app.poll_once() == [target]
    assert runtime.calls == ["prepare", "review", "publish", "cleanup"]


def test_review_application_cleans_up_after_a_failed_graph():
    runtime = FakeReviewRuntime(fail_review=True)
    graph = _review_graph(runtime)
    target = _review_target()
    app = ReviewApplication(
        SimpleNamespace(poll=lambda: [target]),
        runtime=runtime,
        run_workflow=lambda current: graph.invoke(review_steps.seed(current)),
    )

    assert app.poll_once() == []
    assert runtime.calls == ["prepare", "review", "cleanup"]


# --- triage -----------------------------------------------------------------


class FakeTriageExecutor:
    def __init__(self, outcome=None, *, fail: bool = False):
        self.outcome = outcome or TriageOutcome(
            True, True, "high", "ready", (), context=Context()
        )
        self.fail = fail
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("triage executor failed")
        outcome = self.outcome
        return TriageOutcome(
            outcome.success,
            outcome.enough_context,
            outcome.confidence,
            outcome.summary,
            outcome.missing_context,
            context=request.context,
        )


class FakeTriageDestination:
    def __init__(self):
        self.published = []

    def publish(self, target, outcome):
        self.published.append((target, outcome))
        return PublishedTriage("3", provider="fake", context=target.context)


def _triage_target(number: int = 1) -> TriageTarget:
    return TriageTarget(
        f"triage:company/backend#{number}", "company/backend", "Title", "Description",
        context=Context({"github": {"issue_number": number}}),
    )


def _triage_graph(executor, destination, steps=None, deps=None):
    deps = deps or triage_steps.TriageStageDeps(
        executor=executor, destination=destination,
    )
    return build_workflow(
        list(steps or default_steps("triage")), triage_steps.STEP_FACTORIES, deps,
        state_schema=TriageState,
    )


def test_default_triage_graph_assesses_and_publishes():
    executor = FakeTriageExecutor()
    destination = FakeTriageDestination()
    graph = _triage_graph(executor, destination)

    result = graph.invoke(triage_steps.seed(_triage_target()))

    assert result["status"] == COMPLETED
    assert result["output"]["external_id"] == "3"
    assert sorted(result["step_results"]) == ["assess", "publish"]
    assert destination.published[0][0].id == "triage:company/backend#1"


def test_triage_graph_skips_publication_when_assessment_fails():
    executor = FakeTriageExecutor(fail=True)
    destination = FakeTriageDestination()
    graph = _triage_graph(executor, destination)

    result = graph.invoke(triage_steps.seed(_triage_target()))

    assert result["status"] == FAILED
    assert destination.published == []
    assert result["step_results"]["assess"]["status"] == "failed"


def test_triage_step_uses_the_configured_prompt_file_and_executor_override():
    default_executor = FakeTriageExecutor()
    override_executor = FakeTriageExecutor()
    destination = FakeTriageDestination()
    deps = triage_steps.TriageStageDeps(
        executor=default_executor,
        destination=destination,
        step_executors={"assess": override_executor},
        prompts={"assess": "custom triage prompt"},
    )
    graph = _triage_graph(default_executor, destination, deps=deps)

    result = graph.invoke(triage_steps.seed(_triage_target()))

    assert result["status"] == COMPLETED
    assert default_executor.requests == []
    assert override_executor.requests[0].prompt == "custom triage prompt"
    assert override_executor.requests[0].log_file.endswith("assess.log")


def test_triage_application_graph_path_isolates_failed_targets():
    targets = [_triage_target(1), _triage_target(2)]
    calls = []

    class Source:
        def poll(self):
            return targets

    def executor_for(target):
        return FakeTriageExecutor(fail=target.id.endswith("#1"))

    def run_workflow(target):
        executor = executor_for(target)
        destination = FakeTriageDestination()
        calls.append((target.id, destination))
        return _triage_graph(executor, destination).invoke(triage_steps.seed(target))

    app = TriageApplication(
        Source(),
        FakeTriageExecutor(),
        FakeTriageDestination(),
        run_workflow=run_workflow,
    )

    processed = app.poll_once()

    assert [target.id for target in processed] == ["triage:company/backend#2"]
    assert calls[0][0] == "triage:company/backend#1"
    assert calls[1][1].published[0][0].id == "triage:company/backend#2"


# --- discussion -------------------------------------------------------------


class FakeDiscussionRuntime:
    def __init__(self, *, fail_answer: bool = False):
        self.calls: list[str] = []
        self.requests = []
        self.prompts: list[str] = []
        self.fail_answer = fail_answer

    def prepare(self, request):
        self.calls.append("prepare")
        self.requests.append(request)
        return PreparedDiscussion(
            request,
            WorkspaceResult(_WORKSPACE, request.branch, request.context, request.base_branch),
            request.context,
        )

    def answer(self, request, prepared, *, prompt=None, node="discussion"):
        self.calls.append("answer")
        self.prompts.append(prompt or "")
        if self.fail_answer:
            raise RuntimeError("discussion executor failed")
        return DiscussionResult(True, "the answer", context=prepared.context)

    def publish(self, request, result):
        self.calls.append("publish")

    def cleanup(self, prepared):
        self.calls.append("cleanup")

    def task_log_path(self, task_id, node):
        return Path(_WORKSPACE) / f"{task_id}-{node}.log"


def _discussion_seed() -> dict:
    return {
        "task_id": "company/backend#7",
        "input": {
            "provider": "fake",
            "data": {
                "id": "company/backend#7",
                "repository": "company/backend",
                "title": "Title",
                "description": "Body",
                "context": {
                    "git": {
                        "workspace": _WORKSPACE,
                        "branch": "ai/issue-7",
                        "base_branch": "main",
                    }
                },
            },
        },
        "processing": {},
        "workspace": {},
        "output": {},
        "status": "RECEIVED",
    }


def _discussion_graph(runtime, deps=None, steps=None):
    deps = deps or discussion_steps.DiscussionStageDeps(runtime=runtime)
    return build_workflow(
        list(steps or default_steps("discuss")), discussion_steps.STEP_FACTORIES, deps,
        state_schema=DiscussionState,
    )


def test_default_discussion_graph_runs_checkout_answer_publish():
    runtime = FakeDiscussionRuntime()
    graph = _discussion_graph(runtime)

    result = graph.invoke(_discussion_seed())

    assert runtime.calls == ["prepare", "answer", "publish"]
    assert result["status"] == COMPLETED
    assert result["output"]["response"] == "the answer"
    assert sorted(result["step_results"]) == ["answer", "checkout", "publish"]
    assert discussion_steps.prepared_for_cleanup(
        result, list(default_steps("discuss"))
    ) is not None


def test_discussion_step_uses_configured_prompt_and_runtime_override():
    default_runtime = FakeDiscussionRuntime()
    override_runtime = FakeDiscussionRuntime()
    deps = discussion_steps.DiscussionStageDeps(
        runtime=default_runtime,
        step_runtimes={"answer": override_runtime},
        prompts={"answer": "custom discussion prompt"},
    )
    graph = _discussion_graph(default_runtime, deps=deps)

    result = graph.invoke(_discussion_seed())

    assert result["status"] == COMPLETED
    assert default_runtime.calls == ["prepare", "publish"]
    assert override_runtime.calls == ["answer"]
    assert override_runtime.prompts == ["custom discussion prompt"]


def test_cli_discussion_comment_runs_the_graph_and_cleans_up():
    from orchestrator.main import cli

    runtime = FakeDiscussionRuntime()
    steps, deps = list(default_steps("discuss")), discussion_steps.DiscussionStageDeps(
        runtime=runtime
    )

    result = cli._run_discussion_comment(
        _discussion_seed(), "company/backend#7", runtime=runtime, steps=steps, deps=deps,
    )

    assert result["status"] == COMPLETED
    assert result["output"]["response"] == "the answer"
    assert runtime.calls == ["prepare", "answer", "publish", "cleanup"]


def test_cli_discussion_comment_cleans_up_after_a_failed_answer():
    from orchestrator.main import cli

    runtime = FakeDiscussionRuntime(fail_answer=True)
    steps = list(default_steps("discuss"))
    deps = discussion_steps.DiscussionStageDeps(runtime=runtime)

    result = cli._run_discussion_comment(
        _discussion_seed(), "company/backend#7", runtime=runtime, steps=steps, deps=deps,
    )

    assert result["status"] == FAILED
    assert runtime.calls == ["prepare", "answer", "cleanup"]


# --- comment implementation -------------------------------------------------


class FakeCommentRuntime:
    def __init__(self):
        self.prepare_requests = []
        self.implementation_requests = []
        self.published = 0

    def prepare(self, request):
        self.prepare_requests.append(request)
        return SimpleNamespace(
            workspace=WorkspaceResult(_WORKSPACE, "ai/issue-7", request.context, "main"),
            base_branch="main",
            context=request.context,
        )

    def implement_incremental(self, request):
        self.implementation_requests.append(request)
        from orchestrator.application.execution.models import ImplementationResult, PhaseResult

        return ImplementationResult(
            "implemented",
            PhaseResult(ExecutionResult(True, 0, stdout="done", context=request.context), request.context),
        )

    def publish(self, request):
        from orchestrator.application.execution.models import PublishResult
        from orchestrator.domain import PublishedChange

        self.published += 1
        return PublishResult(PublishedChange("11", provider="fake", context=request.context))

    def cleanup(self, request):
        return SimpleNamespace(workspace=request.workspace.workspace)

    def run_command(self, request):
        return SimpleNamespace(result=ExecutionResult(True, 0))

    @property
    def workspace_manager(self):
        return SimpleNamespace(provider_type="fake")

    @property
    def destination(self):
        return SimpleNamespace(provider_type="fake")


def test_cli_incremental_comment_runs_the_configured_graph():
    from orchestrator.infra.langgraph.execution_steps import (
        STEP_FACTORIES,
        ExecutionStageDeps,
    )
    from orchestrator.main import cli

    runtime = FakeCommentRuntime()
    steps = list(default_steps("comment_impl"))
    deps = ExecutionStageDeps(runtime=runtime, reuse_workspace=True)

    result = cli._run_incremental_comment(
        _discussion_seed(), "company/backend#7", runtime=runtime, steps=steps, deps=deps,
    )

    assert result["status"] == COMPLETED
    assert result["output"]["external_id"] == "11"
    assert runtime.prepare_requests[0].reuse_workspace is True
    assert runtime.published == 1
    # STEP_FACTORIES is exercised through the CLI wrapper, not imported only
    # for its side effects.
    assert set(STEP_FACTORIES) >= {"checkout", "implement_comment", "publish", "cleanup"}


def test_comment_impl_graph_reuses_the_workspace_and_uses_prompt_override():
    from orchestrator.infra.langgraph.execution_steps import (
        STEP_FACTORIES,
        ExecutionStageDeps,
    )

    runtime = FakeCommentRuntime()
    steps = list(default_steps("comment_impl"))
    deps = ExecutionStageDeps(
        runtime=runtime,
        prompts={"implement": "custom incremental prompt"},
        reuse_workspace=True,
    )
    graph = build_workflow(steps, STEP_FACTORIES, deps)

    seed = _discussion_seed()
    seed["input"]["event"] = {"metadata": {"instruction": "change it"}}
    result = graph.invoke(seed)

    assert result["status"] == COMPLETED
    assert runtime.prepare_requests[0].reuse_workspace is True
    assert runtime.implementation_requests[0].prompt == "custom incremental prompt"
    assert runtime.implementation_requests[0].node == "implement"
    assert runtime.published == 1
