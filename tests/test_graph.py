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


def _seed():
    return {
        "input": {
            "provider": "fake",
            "data": {
                "id": "repo#1",
                "repository": "company/backend",
                "title": "Add feature",
                "description": "Implement it",
            },
        },
    }


def test_graph_cleans_up_and_keeps_failed_status_after_implementation_error():
    class FailingRuntime(FakeRuntime):
        def implement(self, request):
            self.nodes.append("implement")
            raise RuntimeError("agent failed")

    runtime = FailingRuntime()
    started = []
    graph = build_graph(
        runtime=runtime,
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke(_seed())

    assert started == ["prepare_workspace", "plan", "implement", "cleanup"]
    assert runtime.nodes == started
    assert result["status"] == state_mod.FAILED


def test_graph_cleanup_without_a_prepared_workspace_is_a_noop():
    class BrokenPrepare(FakeRuntime):
        def prepare(self, request):
            self.nodes.append("prepare_workspace")
            raise RuntimeError("clone failed")

    runtime = BrokenPrepare()
    started = []
    graph = build_graph(
        runtime=runtime,
        on_node_start=lambda name, _state: started.append(name),
    )

    result = graph.invoke(_seed())

    assert started == ["prepare_workspace", "cleanup"]
    assert runtime.nodes == ["prepare_workspace"]
    assert result["status"] == state_mod.FAILED


def test_graph_keeps_completed_status_when_cleanup_fails():
    class FailingCleanup(FakeRuntime):
        def cleanup(self, request):
            self.nodes.append("cleanup")
            raise RuntimeError("cleanup failed")

    runtime = FailingCleanup()
    graph = build_graph(runtime=runtime)

    result = graph.invoke(_seed())

    assert "cleanup" in runtime.nodes
    assert result["status"] == state_mod.COMPLETED
