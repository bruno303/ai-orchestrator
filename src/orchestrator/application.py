"""Application services and runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator import config, github, state as state_mod
from orchestrator.github_input import GitHubPollingInputSource, _pr_context_block
from orchestrator.providers import (
    Destination, Executor, InputEvent, InputSource, WorkspaceManager, validate_provider_state,
)


@dataclass(frozen=True)
class Runtime:
    input_source: InputSource
    executor: Executor
    workspace_manager: WorkspaceManager
    destination: Destination
    input_provider: str | None = None


def compose_runtime(store: Any) -> Runtime:
    """Construct the configured provider pipeline with explicit dependencies."""
    pipeline = config.load_pipeline_config()
    return Runtime(
        input_source=config.INPUT_PROVIDERS.create(
            pipeline.input_source.type, {**pipeline.input_source.options, "store": store, "_runtime": True}
        ),
        executor=config.EXECUTOR_PROVIDERS.create(
            pipeline.executor.type, {**pipeline.executor.options, "_runtime": True}
        ),
        workspace_manager=config.WORKSPACE_PROVIDERS.create(
            pipeline.workspace_manager.type, {**pipeline.workspace_manager.options, "_runtime": True}
        ),
        destination=config.DESTINATION_PROVIDERS.create(
            pipeline.destination.type, {**pipeline.destination.options, "_runtime": True}
        ),
        input_provider=pipeline.input_source.type,
    )


def _input_seed(
    event: InputEvent,
    task_id: str,
    *,
    provider: str | None = None,
    extra_context: list[str] | None = None,
) -> dict:
    provider_state = validate_provider_state(event.provider_state)
    data = {"repository": event.repository, "number": event.number, "title": event.title, "body": event.body}
    if extra_context:
        data["extra_context"] = extra_context
    return {
        "task_id": task_id,
        "input": {"provider": provider or event.provider or "github", "data": data, "provider_state": provider_state},
        "processing": {"phase_attempts": 1},
        "workspace": {"branch": f"ai/issue-{event.number}"},
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
        reset_task: Callable[[Any, str, int, str], None],
        github_client: Any = github,
        now: Callable[[], str] | None = None,
        input_provider: str | None = None,
    ) -> None:
        self.store = store
        self.input_source = input_source
        self.run_graph = run_graph
        self.persist_result = persist_result
        self.reset_task = reset_task
        self.github = github_client
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
        task_id = f"{event.repository}#{event.number}"
        # A command comment for the same issue may have been processed earlier
        # in this polling snapshot.
        if self.store.exists(event.repository, event.number):
            return
        seed = _input_seed(event, task_id, provider=self.input_provider or _input_provider(self.input_source))
        print(f"[{self.now()}] new issue: {task_id} - {event.title}")
        self.store.create_task(task_id, event.repository, event.number)
        self.persist_result(self.store, self.run_graph(self.store, seed, task_id))

    def _run_comment(self, event: InputEvent) -> None:
        comment = event.metadata["comment"]
        task_id = f"{event.repository}#{event.number}"
        task = self.store.get_task(task_id)
        if task and task["status"] in self._active_statuses():
            return
        self.store.mark_comment_handled(comment.id, task_id, event.repository, event.number, "STARTED")
        try:
            self.github.add_reaction(event.repository, comment.id, "eyes")
        except self.github.GitHubError:
            pass
        self.reset_task(self.store, event.repository, event.number, f"ai/issue-{event.number}")
        try:
            issue = self.github.get_issue(event.repository, event.number)
        except self.github.GitHubError as exc:
            self.store.update_comment_status(comment.id, state_mod.FAILED)
            try:
                self.github.add_reaction(event.repository, comment.id, "-1")
            except self.github.GitHubError:
                pass
            print(f"[{self.now()}] command comment {comment.id}: could not load issue: {exc}", flush=True)
            return
        pr_number = event.metadata.get("pr_number")
        context: list[str] = []
        if pr_number is None:
            try:
                pr_number = self.github.find_open_pr(event.repository, f"ai/issue-{event.number}")
            except self.github.GitHubError:
                pr_number = None
        if pr_number is not None:
            try:
                context.append(_pr_context_block(self.github.get_pull_request(event.repository, pr_number)))
            except self.github.GitHubError:
                pass
        context.append(comment.body)
        seed = _input_seed(event, task_id, provider=self.input_provider or _input_provider(self.input_source), extra_context=context)
        seed["input"]["data"]["title"] = issue.title
        seed["input"]["data"]["body"] = issue.body
        seed["input"]["data"]["pr_number"] = pr_number
        print(f"[{self.now()}] command comment {comment.id} on {event.repository} ({task_id})", flush=True)
        self.store.create_task(task_id, event.repository, event.number)
        result = self.run_graph(self.store, seed, task_id)
        self.persist_result(self.store, result)
        status = result.get("status", state_mod.FAILED)
        try:
            self.github.add_reaction(event.repository, comment.id, "rocket" if status == state_mod.COMPLETED else "-1")
        except self.github.GitHubError:
            pass
        self.store.update_comment_status(comment.id, status)

    @staticmethod
    def _active_statuses() -> set[str]:
        from orchestrator.github_input import ACTIVE_STATUSES
        return ACTIVE_STATUSES


ApplicationService = PollingApplication
