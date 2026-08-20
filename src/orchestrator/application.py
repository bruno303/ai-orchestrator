"""Application services and runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator import config, state as state_mod
from orchestrator.providers import (
    Destination, Executor, InputEvent, InputSource, SourceFeedback, WorkspaceManager,
    validate_provider_state,
)
from orchestrator.runtime import compose_execution_runtime
from orchestrator.runtime.execution import ExecutionRuntime


@dataclass(frozen=True)
class Runtime:
    input_source: InputSource
    executor: Executor
    workspace_manager: WorkspaceManager
    destination: Destination
    feedback: SourceFeedback | None = None
    input_provider: str | None = None
    execution_runtime: ExecutionRuntime | None = None


def compose_runtime(store: Any) -> Runtime:
    """Construct the configured provider pipeline with explicit dependencies."""
    pipeline = config.load_pipeline_config()
    input_source = config.INPUT_PROVIDERS.create(
        pipeline.input_source.type, {**pipeline.input_source.options, "store": store, "_runtime": True}
    )
    executor = config.EXECUTOR_PROVIDERS.create(
        pipeline.executor.type, {**pipeline.executor.options, "_runtime": True}
    )
    workspace_manager = config.WORKSPACE_PROVIDERS.create(
        pipeline.workspace_manager.type, {**pipeline.workspace_manager.options, "_runtime": True}
    )
    destination = config.DESTINATION_PROVIDERS.create(
        pipeline.destination.type, {**pipeline.destination.options, "_runtime": True}
    )
    return Runtime(
        input_source=input_source,
        executor=executor,
        workspace_manager=workspace_manager,
        destination=destination,
        feedback=getattr(input_source, "feedback", None),
        input_provider=pipeline.input_source.type,
        execution_runtime=compose_execution_runtime(
            executor=executor, workspace_manager=workspace_manager, destination=destination
        ),
    )


def _input_seed(
    event: InputEvent,
    task_id: str,
    *,
    provider: str | None = None,
    extra_context: list[str] | None = None,
) -> dict:
    provider_state = validate_provider_state(event.provider_state)
    data = {
        "repository": event.repository,
        "number": event.number,
        "work_item_id": event.work_item_id or task_id,
        "title": event.title,
        "body": event.body,
    }
    data.update(event.metadata.get("compatibility_data", {}))
    if extra_context or event.extra_context:
        data["extra_context"] = extra_context or event.extra_context
    return {
        "task_id": task_id,
        "input": {"provider": provider or event.provider or "github", "data": data, "provider_state": provider_state},
        "processing": {"phase_attempts": 1},
        "workspace": {
            "branch": provider_state.get("branch", ""),
            "path": provider_state.get("workspace", ""),
            "base_branch": provider_state.get("base_branch", ""),
        },
        "output": {},
        "status": state_mod.RECEIVED,
        "iteration": 1,
        "phase_attempts": 1,
    }


def _input_provider(source: InputSource) -> str:
    return str(getattr(source, "provider_type", getattr(source, "provider_name", "github")))


class PollingApplication:
    """Poll an input source and start the configured workflow for each event."""

    def __init__(
        self,
        store: Any,
        input_source: InputSource,
        run_graph: Callable[[Any, dict, str], dict],
        persist_result: Callable[[Any, dict], None],
        reset_task: Callable[[Any, InputEvent], None],
        feedback: SourceFeedback | None = None,
        now: Callable[[], str] | None = None,
        input_provider: str | None = None,
    ) -> None:
        self.store = store
        self.input_source = input_source
        self.run_graph = run_graph
        self.persist_result = persist_result
        self.reset_task = reset_task
        self.feedback = feedback or getattr(input_source, "feedback", None) or _NoopFeedback()
        self.now = now or (lambda: "--:--:--")
        self.input_provider = input_provider

    def poll_once(self, once: bool = False) -> None:
        for event in self.input_source.poll():
            if event.metadata.get("kind") == "comment":
                self._run_comment(event)
            else:
                self._run_issue(event)
            if once:
                return

    def _run_issue(self, event: InputEvent) -> None:
        task_id = event.work_item_id or (
            f"{event.repository}#{event.number}" if event.number is not None else event.event_id
        )
        # A command comment for the same issue may have been processed earlier
        # in this polling snapshot.
        existing = (
            self.store.exists_task(task_id)
            if hasattr(self.store, "exists_task")
            else self.store.get_task(task_id) is not None
        )
        if existing:
            return
        seed = _input_seed(event, task_id, provider=self.input_provider or _input_provider(self.input_source))
        print(f"[{self.now()}] new issue: {task_id} - {event.title}")
        self.store.create_task(task_id, event.repository, event.number)
        self.persist_result(self.store, self.run_graph(self.store, seed, task_id))

    def _run_comment(self, event: InputEvent) -> None:
        task_id = event.work_item_id or event.event_id
        task = self.store.get_task(task_id)
        if task and task["status"] in self._active_statuses():
            return
        self.feedback.mark_started(event)
        self.reset_task(self.store, event)
        seed = _input_seed(
            event,
            task_id,
            provider=self.input_provider or _input_provider(self.input_source),
        )
        event_reference = event.event_id.rsplit(":", 1)[-1]
        print(
            f"[{self.now()}] command comment {event_reference} "
            f"on {event.repository} ({task_id})",
            flush=True,
        )
        self.store.create_task(task_id, event.repository, event.number)
        try:
            result = self.run_graph(self.store, seed, task_id)
            self.persist_result(self.store, result)
            status = result.get("status", state_mod.FAILED)
            if status == state_mod.COMPLETED:
                self.feedback.mark_succeeded(event)
            else:
                self.feedback.mark_failed(event, result.get("error"))
        except Exception as exc:
            self.feedback.mark_failed(event, str(exc))
            raise

    @staticmethod
    def _active_statuses() -> set[str]:
        return {
            state_mod.RECEIVED,
            state_mod.PREPARING,
            state_mod.PLANNING,
            state_mod.IMPLEMENTING,
            state_mod.TESTING,
            state_mod.CREATING_PR,
        }


ApplicationService = PollingApplication


class _NoopFeedback:
    def mark_started(self, event: InputEvent) -> None:
        return None

    def mark_succeeded(self, event: InputEvent) -> None:
        return None

    def mark_failed(self, event: InputEvent, error: str | None = None) -> None:
        return None
