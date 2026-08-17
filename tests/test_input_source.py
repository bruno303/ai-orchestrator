"""Tests for input-source and application boundaries."""

from orchestrator.application import PollingApplication
from orchestrator.providers import InputEvent


class FakeSource:
    def __init__(self, events):
        self.events = events

    def poll(self):
        return self.events


def test_polling_application_starts_issue_workflow(tmp_path):
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    started = []
    persisted = []
    event = InputEvent("issue:r#1", "r", "Fix", "details", number=1, metadata={"kind": "issue"})

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
    event = InputEvent("issue:r#2", "r", "Fix", number=2, metadata={"kind": "issue"})
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
