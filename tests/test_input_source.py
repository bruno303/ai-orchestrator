"""Tests for stateless polling and GitHub marker semantics."""

from types import SimpleNamespace

from orchestrator.application import PollingApplication
from orchestrator.domain import Context, WorkItem
from orchestrator.infra.github.input import GitHubPollingInputSource, GitHubSourceFeedback
from orchestrator.application.ports import InputEvent


class Source:
    def __init__(self, events): self.events = events
    def poll(self): return self.events


def event(task_id, kind="issue"):
    return InputEvent(f"{kind}:{task_id}", WorkItem(task_id, "owner/repo", "Title", context=Context()),
                      trigger="rerun" if kind == "comment" else "new", metadata={"kind": kind})


def github_issue_event(number):
    task_id = f"owner/repo#{number}"
    return InputEvent(
        f"issue:{task_id}",
        WorkItem(task_id, "owner/repo", f"Issue {number}", context=Context({"github": {"issue_number": number}})),
        trigger="new",
        metadata={"kind": "issue"},
    )


def test_polling_prefers_comment_over_ordinary_issue():
    started = []
    app = PollingApplication(Source([event("owner/repo#1"), event("owner/repo#1", "comment")]),
        lambda seed, task_id: started.append(task_id) or {"task_id": task_id, "status": "COMPLETED"},
        lambda result: None, lambda current: None)
    app.poll_once()
    assert started == ["owner/repo#1"]


def test_polling_only_starts_a_task_once_per_snapshot():
    started = []
    app = PollingApplication(Source([event("owner/repo#1"), event("owner/repo#1")]),
        lambda seed, task_id: started.append(task_id) or {"task_id": task_id, "status": "COMPLETED"},
        lambda result: None, lambda current: None)
    app.poll_once()
    assert started == ["owner/repo#1"]


class Client:
    class GitHubError(Exception): pass
    def __init__(self, reactions=(), issues=None):
        self.reactions = reactions
        self.issues = issues or [
            SimpleNamespace(number=1, title="eligible", body="", labels=["ai-agent"]),
            SimpleNamespace(number=3, title="triage", body="", labels=["ai-triage"]),
            SimpleNamespace(number=2, title="done", body="", labels=["ai-developed"]),
            SimpleNamespace(number=4, title="unlabelled", body="", labels=[]),
        ]
        self.issue_queries = []
        self.assignments = []

    def list_open_issues(self, repository, label=None, assignee=None):
        self.issue_queries.append((repository, label, assignee))
        return self.issues

    def assign_issue_to_authenticated_user(self, repository, number):
        self.assignments.append((repository, number))

    def list_open_pull_requests(self, repository): return []
    def list_issue_comments(self, repository, number): return [SimpleNamespace(id=11, body="/ai-agent rerun")]
    def list_issue_comment_reactions(self, repository, comment_id): return self.reactions
    def get_issue(self, repository, number): return SimpleNamespace(number=number, title="title", body="body")
    def find_open_pr(self, repository, branch): return None


def config_module():
    return SimpleNamespace(allowed_repositories=lambda: ["owner/repo"],
                           repository_command=lambda repo: "/ai-agent")


def test_developed_issue_is_not_returned_as_new_work():
    source = GitHubPollingInputSource(Client(), config_module=config_module())
    assert [item.work_item.id for item in source.poll() if item.metadata["kind"] == "issue"] == ["owner/repo#1"]


def test_polling_uses_unassigned_filter_with_stage_selection_label():
    client = Client()
    source = GitHubPollingInputSource(client, config_module=config_module())

    source.poll()

    assert client.issue_queries == [
        ("owner/repo", None, None),
        ("owner/repo", "ai-agent", "none"),
    ]


def test_polling_does_not_assign_during_discovery():
    client = Client()
    source = GitHubPollingInputSource(client, config_module=config_module())

    events = source.poll()

    assert client.assignments == []
    assert [item.work_item.id for item in events if item.metadata["kind"] == "issue"] == ["owner/repo#1"]


def test_comment_command_bypasses_stage_label_filter():
    client = Client(issues=[SimpleNamespace(number=4, title="unlabelled", body="", labels=[])])
    source = GitHubPollingInputSource(client, config_module=config_module())

    comments = [event for event in source.poll() if event.metadata["kind"] == "comment"]

    assert [event.work_item.id for event in comments] == ["owner/repo#4"]


def test_github_feedback_assigns_new_issue_when_execution_starts():
    client = Client()
    feedback = GitHubSourceFeedback(client)

    feedback.mark_started(github_issue_event(1))

    assert client.assignments == [("owner/repo", 1)]


def test_once_assigns_only_the_issue_that_runs():
    client = Client()
    started = []
    app = PollingApplication(
        Source([github_issue_event(1), github_issue_event(2)]),
        lambda seed, task_id: started.append(task_id) or {"task_id": task_id, "status": "COMPLETED"},
        lambda result: None,
        lambda current: None,
        feedback=GitHubSourceFeedback(client),
    )

    app.poll_once(once=True)

    assert started == ["owner/repo#1"]
    assert client.assignments == [("owner/repo", 1)]


def test_assignment_failure_is_logged_and_next_issue_is_started_in_once_mode(capsys):
    client = Client()
    started = []

    def assign(repository, number):
        if number == 1:
            raise client.GitHubError("permission denied")
        client.assignments.append((repository, number))

    client.assign_issue_to_authenticated_user = assign
    app = PollingApplication(
        Source([github_issue_event(1), github_issue_event(2)]),
        lambda seed, task_id: started.append(task_id) or {"task_id": task_id, "status": "COMPLETED"},
        lambda result: None,
        lambda current: None,
        feedback=GitHubSourceFeedback(client),
        now=lambda: "12:00:00",
    )

    app.poll_once(once=True)

    assert started == ["owner/repo#2"]
    assert client.assignments == [("owner/repo", 2)]
    assert "[12:00:00] new issue owner/repo#1: start failed: permission denied" in capsys.readouterr().out


def test_comment_terminal_reactions_are_filtered_only_for_bot():
    for content, login, expected in (("eyes", "app/bot", True), ("rocket", "app/bot", False),
                                     ("-1", "app/bot", False), ("rocket", "other", True)):
        source = GitHubPollingInputSource(Client([SimpleNamespace(content=content, user_login=login)]),
                                          config_module=config_module(), options={"bot_login": "app/bot"})
        comments = [item for item in source.poll() if item.metadata["kind"] == "comment"]
        assert bool(comments) is expected


def test_comment_reaction_filter_uses_authenticated_client_actor():
    client = Client([SimpleNamespace(content="rocket", user_login="local-user")])
    client.get_authenticated_user_login = lambda: "local-user"
    source = GitHubPollingInputSource(client, config_module=config_module())

    assert not [item for item in source.poll() if item.metadata["kind"] == "comment"]
