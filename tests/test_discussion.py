from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.application import PollingApplication
from orchestrator.application.discussion import DiscussionRunRequest, DiscussionRuntime, discussion_prompt
from orchestrator.application.execution.models import (
    IncrementalExecutionRequest,
    WorkContext,
)
from orchestrator.application.execution.errors import AgentExecutionError, CleanupError, PublicationError
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
from orchestrator.infra.github.input import (
    GitHubPollingInputSource,
    GitHubSourceFeedback,
    parse_comment_command,
)


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
    # Incremental retries must retain the prior worktree so the next agent run
    # can inspect and continue uncommitted changes.
    assert resets == []


def test_incremental_execution_skips_planning(tmp_path):
    requests = []
    cleaned = []
    prepared = []

    class Executor:
        def execute(self, request):
            requests.append(request)
            return ExecutionResult(True, 0, stdout="implemented", context=request.context)

    class Workspace:
        def prepare(self, request):
            prepared.append(request)
            return WorkspaceResult(str(tmp_path), "ai/issue-7", request.context, "main")

        def cleanup(self, result):
            cleaned.append(result)

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
    assert prepared[0].reuse_workspace is True
    assert len(cleaned) == 1


def test_incremental_execution_reports_cleanup_failure_after_publication(tmp_path):
    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(str(tmp_path), "ai/issue-7", request.context, "main")

        def cleanup(self, result):
            raise RuntimeError("workspace cleanup failed")

    class Executor:
        def execute(self, request):
            return ExecutionResult(True, 0, stdout="implemented", context=request.context)

    class Destination:
        def publish(self, request):
            return PublishedChange("17", provider="fake", context=request.context)

    runtime = ExecutionRuntime(Executor(), Workspace(), Destination())

    with pytest.raises(CleanupError, match="workspace cleanup failed"):
        runtime.run_incremental(IncrementalExecutionRequest(
            work=_work(),
            instruction="also validate the email before saving",
            branch="ai/issue-7",
            base_branch="main",
            workspace=str(tmp_path),
            context=_work().item.context,
        ))


def test_incremental_execution_preserves_workspace_when_publication_fails(tmp_path):
    cleaned = []

    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(str(tmp_path), "ai/issue-7", request.context, "main")

        def cleanup(self, result):
            cleaned.append(result)

    class Executor:
        def execute(self, request):
            return ExecutionResult(True, 0, stdout="implemented", context=request.context)

    class Destination:
        def publish(self, request):
            raise RuntimeError("push failed")

    runtime = ExecutionRuntime(Executor(), Workspace(), Destination())

    with pytest.raises(PublicationError, match="push failed"):
        runtime.run_incremental(IncrementalExecutionRequest(
            work=_work(),
            instruction="also validate the email before saving",
            branch="ai/issue-7",
            base_branch="main",
            workspace=str(tmp_path),
            context=_work().item.context,
        ))

    assert cleaned == []


def test_incremental_execution_preserves_workspace_when_implementation_fails(tmp_path):
    cleaned = []

    class Workspace:
        def prepare(self, request):
            return WorkspaceResult(str(tmp_path), "ai/issue-7", request.context, "main")

        def cleanup(self, result):
            cleaned.append(result)

    class Executor:
        def execute(self, request):
            return ExecutionResult(False, 1, stderr="agent failed", context=request.context)

    runtime = ExecutionRuntime(Executor(), Workspace(), object())

    with pytest.raises(AgentExecutionError):
        runtime.run_incremental(IncrementalExecutionRequest(
            work=_work(),
            instruction="also validate the email before saving",
            branch="ai/issue-7",
            base_branch="main",
            workspace=str(tmp_path),
            context=_work().item.context,
        ))

    assert cleaned == []


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


def test_github_discussion_destination_does_not_duplicate_published_response():
    calls = []
    existing = []

    class Client:
        def list_issue_comments(self, repository, number):
            return existing

        def add_issue_comment(self, repository, number, body):
            calls.append((repository, number, body))

    destination = GitHubDiscussionDestination(github_client=Client())
    request = DiscussionPublicationRequest(
        "owner/repo#7:101",
        "owner/repo",
        "The abstraction still holds.",
        Context({"github": {"conversation_number": 42, "comment_id": 101}}),
    )

    destination.publish(request)
    assert len(calls) == 1
    assert "<!-- ai-agent-discussion:101 -->" in calls[0][2]

    existing[:] = [SimpleNamespace(body=calls[0][2])]
    destination.publish(request)

    assert len(calls) == 1


def test_discussion_comment_is_terminal_after_response_without_terminal_reaction():
    class Client:
        class GitHubError(Exception):
            pass

        def __init__(self):
            self.comments = [SimpleNamespace(id=101, body="/ai-agent-discuss explain this")]
            self.reactions = []

        def list_issue_comments(self, repository, number):
            return self.comments

        def list_open_issues(self, repository, label=None, assignee=None):
            return [SimpleNamespace(number=1, title="title", body="body", labels=[])]

        def list_open_pull_requests(self, repository):
            return []

        def list_issue_comment_reactions(self, repository, comment_id):
            return self.reactions

        def add_issue_comment(self, repository, number, body):
            self.comments.append(SimpleNamespace(id=102, body=body))

        def get_issue(self, repository, number):
            return SimpleNamespace(number=number, title="title", body="body")

        def find_open_pr(self, repository, branch):
            return None

    client = Client()
    source = GitHubPollingInputSource(
        client,
        config_module=SimpleNamespace(allowed_repositories=lambda: ["owner/repo"]),
    )
    first = [item for item in source.poll() if item.metadata["kind"] == "comment"]
    assert len(first) == 1

    GitHubDiscussionDestination(github_client=client).publish(
        DiscussionPublicationRequest(
            "owner/repo#1:101",
            "owner/repo",
            "The answer",
            Context({"github": {"conversation_number": 1, "comment_id": 101}}),
        )
    )
    second = [item for item in source.poll() if item.metadata["kind"] == "comment"]

    assert second == []


def test_discussion_publication_is_not_repeated_when_success_reaction_fails():
    class Client:
        class GitHubError(Exception):
            pass

        def __init__(self):
            self.comments = [SimpleNamespace(id=101, body="/ai-agent-discuss explain this")]
            self.reactions = []
            self.publications = 0

        def list_issue_comments(self, repository, number):
            return self.comments

        def list_open_issues(self, repository, label=None, assignee=None):
            return [SimpleNamespace(number=1, title="title", body="body", labels=[])]

        def list_open_pull_requests(self, repository):
            return []

        def list_issue_comment_reactions(self, repository, comment_id):
            return self.reactions

        def add_reaction(self, repository, comment_id, content):
            if content == "rocket":
                raise self.GitHubError("reaction unavailable")

        def add_issue_comment(self, repository, number, body):
            self.publications += 1
            self.comments.append(SimpleNamespace(id=102, body=body))

        def get_issue(self, repository, number):
            return SimpleNamespace(number=number, title="title", body="body")

        def find_open_pr(self, repository, branch):
            return None

    client = Client()
    source = GitHubPollingInputSource(
        client,
        config_module=SimpleNamespace(allowed_repositories=lambda: ["owner/repo"]),
    )
    destination = GitHubDiscussionDestination(github_client=client)

    def run_discussion(seed, task_id):
        destination.publish(DiscussionPublicationRequest(
            task_id,
            "owner/repo",
            "The answer",
            Context.from_dict(seed["input"]["context"]),
        ))
        return {"task_id": task_id, "status": "COMPLETED"}

    app = PollingApplication(
        source,
        lambda *_: {"status": "FAILED"},
        lambda result: None,
        lambda event: None,
        feedback=GitHubSourceFeedback(client),
        run_comment_discuss=run_discussion,
    )

    app.poll_once()
    app.poll_once()

    assert client.publications == 1
