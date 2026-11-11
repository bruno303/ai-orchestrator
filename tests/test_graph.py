"""Graph executes entirely in memory."""

import tempfile

import pytest

from orchestrator.application.execution.models import (
    CleanupResult,
    CommandStepResult,
    ImplementationResult,
    PhaseResult,
    PlanResult,
    PrepareExecutionResult,
    PublishResult,
)
from orchestrator.application.execution.errors import PlanValidationError
from orchestrator.application.ports import ExecutionResult, WorkspaceResult
from orchestrator.application.workflow import StepSpec
from orchestrator.domain import Context, PublishedChange
from orchestrator.infra.langgraph.builder import build_workflow
from orchestrator.infra.langgraph.execution_steps import STEP_FACTORIES, ExecutionStageDeps
from orchestrator.infra.langgraph.graph import build_graph
from orchestrator.infra.langgraph import state as state_mod

# Prepared-workspace directory shared by workspace-consuming graph runs; the
# steps guard on a real existing path (an empty path would mean "right here").
_WORKSPACE_PATH = tempfile.mkdtemp(prefix="orchestrator-graph-tests-")


class FakeRuntime:
    def __init__(self):
        self.nodes = []

        self.workspace_manager = type("WorkspaceManager", (), {"provider_type": "fake"})()
        self.destination = type("Destination", (), {"provider_type": "fake"})()

    def prepare(self, request):
        self.nodes.append("prepare_workspace")
        context = request.context
        workspace = WorkspaceResult(_WORKSPACE_PATH, "ai/issue-1", context, "main")
        return PrepareExecutionResult(request.work, workspace, "main", context)

    def plan(self, request):
        self.nodes.append("plan")
        phase = PhaseResult(ExecutionResult(True, 0, stdout="planned", context=request.context), request.context)
        return PlanResult("planned", ".agents/plans/plan.md", phase)

    def implement(self, request):
        self.nodes.append("implement")
        phase = PhaseResult(ExecutionResult(True, 0, stdout="implemented", context=request.context), request.context)
        return ImplementationResult("implemented", phase)

    def publish(self, request):
        self.nodes.append("create_pr")
        return PublishResult(PublishedChange("17", provider="fake", context=request.context))

    def cleanup(self, request):
        self.nodes.append("cleanup")
        return CleanupResult(request.workspace.workspace)

def test_graph_compiles_without_a_checkpointer():
    assert build_graph(runtime=object()) is not None


def test_graph_routes_implementation_directly_to_publication():
    runtime = FakeRuntime()
    started = []
    graph = build_graph(
        runtime=runtime,
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke({
        "input": {
            "provider": "fake",
            "data": {
                "id": "repo#1",
                "repository": "company/backend",
                "title": "Add feature",
                "description": "Implement it",
            },
        },
    })

    assert started == ["prepare_workspace", "plan", "implement", "create_pr", "cleanup"]
    assert runtime.nodes == started
    assert result["status"] == state_mod.COMPLETED
    assert result["output"]["external_id"] == "17"


# --- generic builder coverage (Task 3) ---


def _seed(**data) -> dict:
    prepared_workspace = data.pop("prepared_workspace", None)
    payload = {
        "id": "repo#1",
        "repository": "company/backend",
        "title": "Add feature",
        "description": "Implement it",
        **data,
    }
    seed = {"input": {"provider": "fake", "data": payload}}
    if prepared_workspace is not None:
        # A checkout-less state that already holds a prepared workspace path.
        seed["workspace"] = {"path": prepared_workspace}
    return seed


class FailingPlanRuntime(FakeRuntime):
    """Plan step raises a typed runtime error (missing plan artifact)."""

    def plan(self, request):
        raise PlanValidationError(
            "planning completed without creating .agents/plans/plan.md",
            context=Context({"opencode": {"session": "s1"}}),
        )


class FailingCleanupRuntime(FakeRuntime):
    """Cleanup step raises, exercising the on_failure=continue policy."""

    def cleanup(self, request):
        raise RuntimeError("could not remove worktree")


class CommandRuntime:
    """Fake command backend for the generic command step."""

    def __init__(self, exit_code, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.requests = []

    def run_command(self, request):
        self.requests.append(request)
        return CommandStepResult(ExecutionResult(
            self.exit_code == 0, self.exit_code,
            stdout=self.stdout, stderr=self.stderr, context=request.context,
        ))


class AgentRuntime:
    """Fake agent-phase runner for the generic agent step."""

    def __init__(self):
        self.requests = []

    def execute_phase(self, request):
        self.requests.append(request)
        return PhaseResult(
            ExecutionResult(True, 0, stdout="agent output", context=request.context),
            request.context,
        )


def test_build_workflow_rejects_empty_step_lists():
    with pytest.raises(ValueError, match="at least one step"):
        build_workflow([], STEP_FACTORIES, ExecutionStageDeps(runtime=FakeRuntime()))


def test_build_workflow_rejects_unknown_step_types():
    deps = ExecutionStageDeps(runtime=FakeRuntime())

    with pytest.raises(ValueError, match="step 'mystery': unknown step type 'bogus'"):
        build_workflow([StepSpec("mystery", "bogus")], STEP_FACTORIES, deps)


def test_build_workflow_rejects_duplicate_step_names():
    deps = ExecutionStageDeps(runtime=FakeRuntime())
    steps = [StepSpec("plan", "plan"), StepSpec("plan", "publish")]

    with pytest.raises(ValueError, match="step 'plan': duplicate step name"):
        build_workflow(steps, STEP_FACTORIES, deps)


def test_build_workflow_executes_a_custom_step_sequence():
    runtime = FakeRuntime()
    started = []
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("plan", "plan"),
        StepSpec("publish", "publish"),
    ]
    graph = build_workflow(
        steps, STEP_FACTORIES, ExecutionStageDeps(runtime=runtime),
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke(_seed())

    assert started == ["checkout", "plan", "publish"]
    assert runtime.nodes == ["prepare_workspace", "plan", "create_pr"]
    assert result["status"] == state_mod.COMPLETED
    assert sorted(result["step_results"]) == ["checkout", "plan", "publish"]
    assert result["step_results"]["plan"]["status"] == "succeeded"


def test_renamed_plan_step_uses_its_name_for_executor_logs():
    class RecordingPlanRuntime(FakeRuntime):
        def plan(self, request):
            self.plan_node = request.node
            return super().plan(request)

    runtime = RecordingPlanRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("analyse", "plan"),
        StepSpec("publish", "publish"),
    ]
    graph = build_workflow(steps, STEP_FACTORIES, ExecutionStageDeps(runtime=runtime))

    graph.invoke(_seed())

    assert runtime.plan_node == "analyse"


def test_build_workflow_aborts_when_the_plan_step_fails():
    runtime = FailingPlanRuntime()
    started = []
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("plan", "plan"),
        StepSpec("publish", "publish"),
    ]
    graph = build_workflow(
        steps, STEP_FACTORIES, ExecutionStageDeps(runtime=runtime),
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke(_seed())

    assert started == ["checkout", "plan"]  # publish never executed
    assert result["status"] == state_mod.FAILED
    assert result["error"] == "planning completed without creating .agents/plans/plan.md"
    assert result["processing"]["context"] == {"opencode": {"session": "s1"}}
    plan_result = result["step_results"]["plan"]
    assert plan_result["status"] == "failed"
    assert plan_result["error"] == "planning completed without creating .agents/plans/plan.md"
    assert plan_result["duration_s"] >= 0
    assert "publish" not in result["step_results"]


def test_cleanup_failure_with_continue_reaches_end_and_stays_completed():
    started = []
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("publish", "publish"),
        StepSpec("housekeeping", "cleanup", on_failure="continue"),
    ]
    graph = build_workflow(
        steps, STEP_FACTORIES, ExecutionStageDeps(runtime=FailingCleanupRuntime()),
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke(_seed())

    assert started == ["checkout", "publish", "housekeeping"]
    assert result["status"] == state_mod.COMPLETED
    assert "error" not in result
    entry = result["step_results"]["housekeeping"]
    assert entry["status"] == "failed"
    assert "could not remove worktree" in entry["error"]
    assert "duration_s" in entry
    assert sorted(result["step_results"]) == ["checkout", "housekeeping", "publish"]


def test_step_runtimes_route_matching_steps_to_the_override_runtime():
    default_runtime = FakeRuntime()
    other_runtime = FakeRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("plan", "plan"),
        StepSpec("publish", "publish"),
    ]
    deps = ExecutionStageDeps(
        runtime=default_runtime, step_runtimes={"plan": other_runtime},
    )
    graph = build_workflow(steps, STEP_FACTORIES, deps)

    result = graph.invoke(_seed())

    assert other_runtime.nodes == ["plan"]
    assert default_runtime.nodes == ["prepare_workspace", "create_pr"]
    assert result["status"] == state_mod.COMPLETED
    assert result["step_results"]["plan"]["status"] == "succeeded"


def test_command_step_reports_success_snippets_and_processing_context():
    assert ExecutionStageDeps(runtime=FakeRuntime()).command_timeout_seconds == 3600
    command_runtime = CommandRuntime(0, stdout="all tests passed")
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("lint", "command", command="ruff check ."),
        StepSpec("tests", "command", command="pytest -q", timeout_seconds=60),
    ]
    deps = ExecutionStageDeps(
        runtime=FakeRuntime(),
        step_runtimes={"lint": command_runtime, "tests": command_runtime},
        command_timeout_seconds=30,
    )
    graph = build_workflow(steps, STEP_FACTORIES, deps)

    result = graph.invoke(_seed())

    assert result["step_results"]["lint"]["status"] == "succeeded"
    assert result["step_results"]["lint"]["summary"] == "all tests passed"
    assert result["step_results"]["tests"]["status"] == "succeeded"
    assert result["processing"]["context"] == {}
    requests = {request.node: request for request in command_runtime.requests}
    assert requests["lint"].command == "ruff check ."
    assert requests["lint"].timeout_seconds == 30  # step timeout falls back to deps default
    assert requests["tests"].timeout_seconds == 60  # explicit step timeout wins
    assert requests["tests"].workspace == _WORKSPACE_PATH


def test_command_step_failure_aborts_the_run():
    command_runtime = CommandRuntime(2, stderr="boom")
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("tests", "command", command="pytest -q"),
    ]
    deps = ExecutionStageDeps(
        runtime=FakeRuntime(), step_runtimes={"tests": command_runtime},
    )
    graph = build_workflow(steps, STEP_FACTORIES, deps)

    result = graph.invoke(_seed())

    assert result["status"] == state_mod.FAILED
    entry = result["step_results"]["tests"]
    assert entry["status"] == "failed"
    assert entry["error"] == "unhandled exception in tests: command step 'tests' failed (exit 2)"
    assert "summary" not in entry


def test_agent_step_renders_the_template_and_uses_the_step_agent():
    document_runtime = AgentRuntime()
    steps = [
        StepSpec("checkout", "checkout"),
        StepSpec("document", "agent", agent="impl"),
    ]
    deps = ExecutionStageDeps(
        runtime=FakeRuntime(),
        step_runtimes={"document": document_runtime},
        prompts={"document": "Answer {task_id} about {title}"},
    )
    graph = build_workflow(steps, STEP_FACTORIES, deps)

    result = graph.invoke(_seed())

    request = document_runtime.requests[0]
    assert request.agent == "impl"
    assert request.node == "document"
    assert request.prompt == "Answer repo#1 about Add feature"
    assert request.workspace == _WORKSPACE_PATH
    assert request.work.task_id == "repo#1"
    assert result["step_results"]["document"]["status"] == "succeeded"
    assert result["step_results"]["document"]["summary"] == "agent output"


def test_agent_step_defaults_to_the_build_agent_without_override():
    document_runtime = AgentRuntime()
    deps = ExecutionStageDeps(
        runtime=document_runtime,
        prompts={"document": "Answer {task_id} about {title}"},
    )
    graph = build_workflow([StepSpec("document", "agent")], STEP_FACTORIES, deps)

    result = graph.invoke(_seed(prepared_workspace=_WORKSPACE_PATH))

    assert document_runtime.requests[0].agent == "build"
    assert document_runtime.requests[0].prompt == "Answer repo#1 about Add feature"
    assert result["step_results"]["document"]["status"] == "succeeded"


def test_agent_step_renders_description_and_joined_extra_context():
    document_runtime = AgentRuntime()
    deps = ExecutionStageDeps(
        runtime=document_runtime,
        prompts={"document": "Add {description} ({extra_context})"},
    )
    graph = build_workflow([StepSpec("document", "agent")], STEP_FACTORIES, deps)

    result = graph.invoke(_seed(prepared_workspace=_WORKSPACE_PATH, extra_context=("first note", "second note")))

    assert document_runtime.requests[0].prompt == "Add Implement it (first note\nsecond note)"
    assert result["step_results"]["document"]["status"] == "succeeded"


def test_agent_step_without_a_prompt_template_fails_the_step():
    # Defensive runtime check for directly-built graphs: validate_steps now
    # rejects prompt-less `agent` steps at configuration load time.
    document_runtime = AgentRuntime()
    deps = ExecutionStageDeps(runtime=document_runtime)
    graph = build_workflow([StepSpec("document", "agent")], STEP_FACTORIES, deps)

    result = graph.invoke(_seed())

    assert result["status"] == state_mod.FAILED
    entry = result["step_results"]["document"]
    assert entry["status"] == "failed"
    assert "agent step 'document' has no prompt template" in entry["error"]


def test_agent_step_with_literal_braces_fails_with_the_escaping_rule():
    document_runtime = AgentRuntime()
    deps = ExecutionStageDeps(
        runtime=document_runtime,
        prompts={"document": "awk '{print $1}' for {task_id}"},
    )
    graph = build_workflow([StepSpec("document", "agent")], STEP_FACTORIES, deps)

    result = graph.invoke(_seed())

    assert result["status"] == state_mod.FAILED
    entry = result["step_results"]["document"]
    assert entry["status"] == "failed"
    assert "'print $1'" in entry["error"]
    assert "'{{' renders '{'" in entry["error"]
    assert document_runtime.requests == []  # the agent never started


def test_workspace_step_without_a_prepared_workspace_fails_cleanly():
    runtime = FakeRuntime()
    started = []
    steps = [
        StepSpec("plan", "plan"),
        StepSpec("implement", "implement"),
        StepSpec("publish", "publish"),
        StepSpec("cleanup", "cleanup", on_failure="continue"),
    ]
    graph = build_workflow(
        steps, STEP_FACTORIES, ExecutionStageDeps(runtime=runtime),
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke(_seed())

    message = "step 'plan' requires a prepared workspace; run a checkout step first"
    assert result["status"] == state_mod.FAILED
    assert result["error"] == message
    entry = result["step_results"]["plan"]
    assert entry["status"] == "failed"
    assert entry["error"] == message
    assert started == ["plan"]  # publish and cleanup never executed
    assert runtime.nodes == []


def test_command_step_without_a_prepared_workspace_fails_cleanly():
    command_runtime = CommandRuntime(0, stdout="ok")
    deps = ExecutionStageDeps(
        runtime=FakeRuntime(), step_runtimes={"lint": command_runtime},
    )
    graph = build_workflow(
        [StepSpec("lint", "command", command="pwd")], STEP_FACTORIES, deps,
    )

    result = graph.invoke(_seed())

    assert result["status"] == state_mod.FAILED
    entry = result["step_results"]["lint"]
    assert entry["status"] == "failed"
    assert "step 'lint' requires a prepared workspace" in entry["error"]
    assert command_runtime.requests == []  # nothing ran in the orchestrator's cwd


def test_on_node_start_errors_are_swallowed():
    def raising_callback(_name, _state):
        raise RuntimeError("callback failed")

    graph = build_workflow(
        [StepSpec("checkout", "checkout")], STEP_FACTORIES,
        ExecutionStageDeps(runtime=FakeRuntime()), on_node_start=raising_callback,
    )

    result = graph.invoke(_seed())

    assert result["status"] == state_mod.PREPARING
    assert result["step_results"]["checkout"]["status"] == "succeeded"
