"""Application services and runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator import config, state as state_mod
from orchestrator.domain import Context, ContextPresenter, NoopContextPresenter
from orchestrator.providers import (
    Destination, Executor, InputEvent, InputSource, SourceFeedback, WorkspaceManager,
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
    context_presenter: ContextPresenter = NoopContextPresenter()


def compose_runtime() -> Runtime:
    """Construct the configured provider pipeline with explicit dependencies."""
    pipeline = config.load_pipeline_config()
    input_source = config.INPUT_PROVIDERS.create(
        pipeline.input_source.type, {**pipeline.input_source.options, "_runtime": True}
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
        context_presenter=getattr(input_source, "context_presenter", NoopContextPresenter()),
    )


def _input_seed(
    event: InputEvent,
    task_id: str,
    *,
    provider: str | None = None,
    extra_context: list[str] | None = None,
) -> dict:
    item = event.work_item
    context = item.context.merged(event.context)
    data = {
        "id": item.id,
        "work_item_id": item.id,
        "repository": item.repository,
        "title": item.title,
        "description": item.description,
        "extra_context": list(item.extra_context),
        "input_provider": provider or item.input_provider,
        "context": context.to_dict(),
    }
    if extra_context:
        data["extra_context"] = extra_context
    return {
        "task_id": task_id,
        "input": {"provider": provider or item.input_provider, "data": data, "context": context.to_dict()},
        "processing": {"phase_attempts": 1},
        "workspace": {},
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
        input_source: InputSource,
        run_graph: Callable[[dict, str], dict],
        report_result: Callable[[dict], None],
        reset_task: Callable[[InputEvent], None],
        feedback: SourceFeedback | None = None,
        now: Callable[[], str] | None = None,
        input_provider: str | None = None,
    ) -> None:
        self.input_source = input_source
        self.run_graph = run_graph
        self.report_result = report_result
        self.reset_task = reset_task
        self.feedback = feedback or getattr(input_source, "feedback", None) or _NoopFeedback()
        self.now = now or (lambda: "--:--:--")
        self.input_provider = input_provider

    def poll_once(self, once: bool = False) -> None:
        events = self.input_source.poll()
        # Explicit commands override ordinary issue discovery in one snapshot.
        command_task_ids = {
            event.work_item.id for event in events
            if event.trigger == "rerun" or event.metadata.get("kind") == "comment"
        }
        started: set[str] = set()
        for event in events:
            task_id = event.work_item.id
            is_comment = event.trigger == "rerun" or event.metadata.get("kind") == "comment"
            if task_id in started or (not is_comment and task_id in command_task_ids):
                continue
            started.add(task_id)
            if is_comment:
                self._run_comment(event)
            else:
                self._run_issue(event)
            if once:
                return

    def _run_issue(self, event: InputEvent) -> None:
        task_id = event.work_item.id
        seed = _input_seed(event, task_id, provider=self.input_provider or _input_provider(self.input_source))
        print(f"[{self.now()}] new issue: {task_id} - {event.work_item.title}")
        self.report_result(self.run_graph(seed, task_id))

    def _run_comment(self, event: InputEvent) -> None:
        task_id = event.work_item.id
        self.feedback.mark_started(event)
        self.reset_task(event)
        seed = _input_seed(
            event,
            task_id,
            provider=self.input_provider or _input_provider(self.input_source),
        )
        event_reference = event.event_id.rsplit(":", 1)[-1]
        print(
            f"[{self.now()}] command comment {event_reference} "
            f"on {event.work_item.repository} ({task_id})",
            flush=True,
        )
        try:
            result = self.run_graph(seed, task_id)
            self.report_result(result)
            status = result.get("status", state_mod.FAILED)
            if status == state_mod.COMPLETED:
                self.feedback.mark_succeeded(event)
            else:
                self.feedback.mark_failed(event, result.get("error"))
        except Exception as exc:
            self.feedback.mark_failed(event, str(exc))
            raise

ApplicationService = PollingApplication


class _NoopFeedback:
    def mark_started(self, event: InputEvent) -> None:
        return None

    def mark_succeeded(self, event: InputEvent) -> None:
        return None

    def mark_failed(self, event: InputEvent, error: str | None = None) -> None:
        return None
