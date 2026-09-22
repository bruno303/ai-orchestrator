"""Graph executes entirely in memory."""

from orchestrator.application.execution.models import (
    CleanupResult,
    ImplementationResult,
    PhaseResult,
    PlanResult,
    PrepareExecutionResult,
    PublishResult,
)
from orchestrator.application.ports import ExecutionResult, WorkspaceResult
from orchestrator.domain import PublishedChange
from orchestrator.infra.langgraph.graph import build_graph
from orchestrator.infra.langgraph import state as state_mod


class FakeRuntime:
    def __init__(self):
        self.nodes = []

        self.workspace_manager = type("WorkspaceManager", (), {"provider_type": "fake"})()
        self.destination = type("Destination", (), {"provider_type": "fake"})()

    def prepare(self, request):
        self.nodes.append("prepare_workspace")
        context = request.context
        workspace = WorkspaceResult("/tmp/workspace", "ai/issue-1", context, "main")
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

class FailingCleanupRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.reported = []

    def cleanup(self, request):
        self.nodes.append("cleanup")
        raise RuntimeError("worktree removal failed")

    def report_cleanup_error(self, task_id, error):
        self.reported.append((task_id, error))


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


def test_graph_cleanup_failure_reports_and_stays_completed():
    runtime = FailingCleanupRuntime()
    graph = build_graph(runtime=runtime)

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

    # A cleanup failure must not flip an already-published task to FAILED;
    # it is reported through the injected reporter instead.
    assert result["status"] == state_mod.COMPLETED
    assert runtime.nodes[-1] == "cleanup"
    assert len(runtime.reported) == 1
    task_id, error = runtime.reported[0]
    assert task_id == "repo#1"
    assert "worktree removal failed" in str(error)
