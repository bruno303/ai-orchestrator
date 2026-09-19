from __future__ import annotations

from orchestrator.application import PollingApplication
from orchestrator.application.discussion import DiscussionRunRequest, DiscussionRuntime, discussion_prompt
from orchestrator.application.execution.models import (
    IncrementalExecutionRequest,
    WorkContext,
)
from orchestrator.application.execution.service import ExecutionRuntime
from orchestrator.application.ports import (
    DiscussionPublicationRequest,
    DiscussionResult,
    ExecutionResult,
    InputEvent,
    WorkspaceResult,
)
from orchestrator.domain import Context, PublishedChange, WorkItem
from orchestrator.infra.github.discussion import GitHubDiscussionDestination
from orchestrator.infra.github.input import parse_comment_command


def _work(task_id: str = "owner/repo#7") -> WorkContext:
    return WorkContext(WorkItem(
        task_id,
        "owner/repo",
        "Add validation",
        "Validate the email before saving.",
        ("/ai-agent-discuss does the abstraction still make sense?",),
        context=Context({
            "github": {"conversation_number": 42},
            "git": {"repository_url": "https://github.com/owner/repo.git", "base_branch": "main"},
        }),
    ))


def test_comment_parser_has_only_explicit_commands():
    assert parse_comment_command("/ai-agent-impl also validate the email") == (
        "impl", "also validate the email"
    )
    assert parse_comment_command("/ai-agent-discuss does this still make sense?") == (
        "discuss", "does this still make sense?"
    )
    assert parse_comment_command("/ai-agent do it") is None
    assert parse_comment_command("/ai-agent-implementation do it") is None


def test_polling_routes_comment_intents_to_dedicated_callbacks():
    impl_work = _work()
    discuss_work = _work("owner/repo#8")
    impl_event = InputEvent(
        "comment:1", impl_work.item, trigger="comment",
        metadata={"kind": "comment", "intent": "impl", "instruction": "change it"},
    )
    discuss_event = InputEvent(
        "comment:2", discuss_work.item, trigger="comment",
        metadata={"kind": "comment", "intent": "discuss", "instruction": "explain it"},
    )
    calls = []
    resets = []
    app = PollingApplication(
        type("Source", (), {"poll": lambda self: [impl_event, discuss_event]})(),
        lambda *_: calls.append("graph") or {"status": "FAILED"},
        lambda result: None,
        lambda event: resets.append(event.event_id),
        run_comment_impl=lambda *_: calls.append("impl") or {"status": "COMPLETED"},
        run_comment_discuss=lambda *_: calls.append("discuss") or {"status": "COMPLETED"},
    )

    app.poll_once()

    assert calls == ["impl", "discuss"]
    assert resets == ["comment:1"]


def test_incremental_execution_skips_planning(tmp_path):
    requests = []

    class Executor:
        def execute(self, request):
            requests.append(request)
            return ExecutionResult(True, 0, stdout="implemented", context=request.context)

    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(str(tmp_path), "ai/issue-7", request.context, "main")

        def cleanup(self, result):
            pass

    class Destination:
        def publish(self, request):
            return PublishedChange("17", provider="fake", context=request.context)

    runtime = ExecutionRuntime(Executor(), Workspace(), Destination())
    runtime.run_incremental(IncrementalExecutionRequest(
        work=_work(),
        instruction="also validate the email before saving",
        branch="ai/issue-7",
        base_branch="main",
        workspace=str(tmp_path),
        context=_work().item.context,
    ))

    assert [request.agent for request in requests] == ["build"]
    assert requests[0].context == _work().item.context
    assert "also validate the email before saving" in requests[0].prompt
    assert "Do not stop after writing a plan" in requests[0].prompt
    assert "/plan-implementation" not in requests[0].prompt


def test_discussion_runtime_is_read_only_and_publishes_only_response(tmp_path):
    captured = {}

    class Workspace:
        def prepare(self, request):
            captured["workspace_request"] = request
            return WorkspaceResult(str(tmp_path), "", request.context, "main")

        def cleanup(self, result):
            captured["cleaned"] = True

    class Executor:
        def execute(self, request):
            captured["request"] = request
            return DiscussionResult(
                True, "The abstraction still holds.", stderr="provider diagnostic", context=request.context
            )

    class Destination:
        def publish(self, request):
            captured["publication"] = request

    runtime = DiscussionRuntime(Executor(), Workspace(), Destination())
    result = runtime.run(DiscussionRunRequest(work=_work(), workspace=str(tmp_path)))

    assert result.response == "The abstraction still holds."
    assert captured["workspace_request"].purpose == "discussion"
    assert captured["workspace_request"].checkout_mode == "revision"
    assert "read-only" in captured["request"].prompt
    assert "Do not modify files" in captured["request"].prompt
    assert "/plan-implementation" in discussion_prompt(_work())
    assert captured["publication"].response == result.response
    assert "provider diagnostic" not in captured["publication"].response
    assert captured["publication"].context.namespace("github")["conversation_number"] == 42
    assert captured["cleaned"] is True


def test_github_discussion_destination_posts_only_to_originating_conversation():
    calls = []

    class Client:
        def add_issue_comment(self, repository, number, body):
            calls.append((repository, number, body))

    GitHubDiscussionDestination(github_client=Client()).publish(
        DiscussionPublicationRequest(
            "owner/repo#7",
            "owner/repo",
            "The abstraction still holds.",
            Context({"github": {"conversation_number": 42}}),
        )
    )

    assert calls == [("owner/repo", 42, "The abstraction still holds.")]
