"""Tests for input-source and application boundaries."""

from orchestrator.application import PollingApplication
from orchestrator.domain import Context, WorkItem
from orchestrator.providers import InputEvent


class FakeSource:
    def __init__(self, events):
        self.events = events

    def poll(self):
        return self.events


def issue_event(event_id: str, task_id: str, number: int | None, *, kind: str = "issue") -> InputEvent:
    return InputEvent(
        event_id,
        WorkItem(task_id, task_id.split("#")[0], "Fix", context=Context({"github": {"issue_number": number}})),
        metadata={"kind": kind},
    )


def test_polling_application_starts_issue_workflow(tmp_path):
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    started = []
    persisted = []
    event = issue_event("issue:r#1", "r#1", 1)

    app = PollingApplication(
        store,
        FakeSource([event]),
        lambda current_store, seed, task_id: started.append((seed, task_id)) or {"task_id": task_id, "status": "FAILED"},
        lambda current_store, result: persisted.append(result),
        lambda *args: None,
    )
    app.poll_once(once=True)

    assert started[0][1] == "r#1"
    assert persisted[0]["task_id"] == "r#1"
    assert store.get_task("r#1") is not None


def test_configured_input_provider_identity_is_persisted(tmp_path):
    from orchestrator.persistence import TaskStore

    class CustomSource(FakeSource):
        provider_type = "custom_input"

    store = TaskStore(tmp_path / "db.sqlite")
    seeds = []
    event = issue_event("issue:r#2", "r#2", 2)
    app = PollingApplication(
        store,
        CustomSource([event]),
        lambda current_store, seed, task_id: seeds.append(seed) or {"task_id": task_id, "status": "FAILED"},
        lambda current_store, result: None,
        lambda *args: None,
    )

    app.poll_once(once=True)

    assert seeds[0]["input"]["provider"] == "custom_input"
    assert "provider" not in seeds[0]


def test_issue_event_is_skipped_if_task_was_created_by_an_earlier_event(tmp_path):
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    started = []
    store.create_task("r#3", "r", 3)
    event = issue_event("issue:r#3", "r#3", 3)

    app = PollingApplication(
        store,
        FakeSource([event]),
        lambda *args: started.append(args),
        lambda *args: None,
        lambda *args: None,
    )
    app.poll_once(once=True)

    assert started == []


def test_provider_neutral_comment_event_uses_its_work_item_id(tmp_path):
    from orchestrator.persistence import TaskStore

    class Feedback:
        def __init__(self):
            self.events = []

        def mark_started(self, event):
            self.events.append(("started", event.event_id))

        def mark_succeeded(self, event):
            self.events.append(("succeeded", event.event_id))

        def mark_failed(self, event, error=None):
            self.events.append(("failed", event.event_id, error))

    store = TaskStore(tmp_path / "db.sqlite")
    reset = []
    started = []
    feedback = Feedback()
    event = InputEvent(
        "comment:external-1",
        WorkItem("external-work-42", "r", "Follow up"),
        metadata={"kind": "comment"},
    )
    app = PollingApplication(
        store,
        FakeSource([event]),
        lambda current_store, seed, task_id: started.append((seed, task_id)) or {
            "task_id": task_id, "status": "COMPLETED"
        },
        lambda *args: None,
        lambda current_store, current_event: reset.append(current_event.event_id),
        feedback=feedback,
    )

    app.poll_once(once=True)

    assert started[0][1] == "external-work-42"
    assert started[0][0]["input"]["data"]["work_item_id"] == "external-work-42"
    assert store.get_task("external-work-42") is not None
    assert reset == ["comment:external-1"]
    assert feedback.events == [
        ("started", "comment:external-1"),
        ("succeeded", "comment:external-1"),
    ]


def test_provider_neutral_issue_without_number_is_accepted(tmp_path):
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    started = []
    event = InputEvent("issue:unknown", WorkItem("issue:unknown", "r", "Fix"), metadata={"kind": "issue"})
    app = PollingApplication(
        store,
        FakeSource([event]),
        lambda *args: started.append(args),
        lambda *args: None,
        lambda *args: None,
    )

    app.poll_once(once=True)

    assert started[0][2] == "issue:unknown"
    assert store.get_task("issue:unknown") is not None


def test_review_input_filters_processed_label():
    from types import SimpleNamespace

    from orchestrator.github_review import GitHubReviewInputSource

    class Client:
        def list_open_pull_requests(self, repository):
            return [SimpleNamespace(number=1), SimpleNamespace(number=2)]

        def get_pull_request(self, repository, number):
            return SimpleNamespace(
                number=number, title="Review", body="", url="url",
                labels=["ai-reviewed"] if number == 1 else [],
                head_ref="head", base_ref="main", head_sha="sha",
                head_clone_url="clone", files=[], changed_lines={},
            )

    source = GitHubReviewInputSource(Client(), SimpleNamespace(allowed_repositories=lambda: ["r"]))
    assert [event.context.namespace("github")["pr_number"] for event in source.poll()] == [2]
