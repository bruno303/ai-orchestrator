"""Tests for stateless polling and GitHub marker semantics."""

from types import SimpleNamespace

from orchestrator.application import PollingApplication
from orchestrator.domain import Context, WorkItem
from orchestrator.github_input import GitHubPollingInputSource
from orchestrator.providers import InputEvent


class Source:
    def __init__(self, events): self.events = events
    def poll(self): return self.events


def event(task_id, kind="issue"):
    return InputEvent(f"{kind}:{task_id}", WorkItem(task_id, "owner/repo", "Title", context=Context()),
                      trigger="rerun" if kind == "comment" else "new", metadata={"kind": kind})


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
    def __init__(self, reactions=()): self.reactions = reactions
    def list_open_issues(self, repository):
        return [SimpleNamespace(number=1, title="eligible", body="", labels=[]),
                SimpleNamespace(number=2, title="done", body="", labels=["ai-developed"])]
    def list_open_pull_requests(self, repository): return []
    def list_issue_comments(self, repository, number): return [SimpleNamespace(id=11, body="/ai-agent rerun")]
    def list_issue_comment_reactions(self, repository, comment_id): return self.reactions
    def get_issue(self, repository, number): return SimpleNamespace(number=number, title="title", body="body")
    def find_open_pr(self, repository, branch): return None


def config_module():
    return SimpleNamespace(allowed_repositories=lambda: ["owner/repo"], repository_label=lambda repo: None,
                           repository_command=lambda repo: "/ai-agent")


def test_developed_issue_is_not_returned_as_new_work():
    source = GitHubPollingInputSource(Client(), config_module=config_module())
    assert [item.work_item.id for item in source.poll() if item.metadata["kind"] == "issue"] == ["owner/repo#1"]


def test_comment_terminal_reactions_are_filtered_only_for_bot():
    for content, login, expected in (("eyes", "app/bot", True), ("rocket", "app/bot", False),
                                     ("-1", "app/bot", False), ("rocket", "other", True)):
        source = GitHubPollingInputSource(Client([SimpleNamespace(content=content, user_login=login)]),
                                          config_module=config_module(), options={"bot_login": "app/bot"})
        comments = [item for item in source.poll() if item.metadata["kind"] == "comment"]
        assert bool(comments) is expected
