"""Provider-neutral pull-request review application service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator import config
from orchestrator.providers import ReviewDestination, ReviewEvent, ReviewExecutor, ReviewInputSource, ReviewRequest, WorkspaceManager, WorkspaceRequest


REVIEW_PROMPT = """Review this pull request. Inspect the complete diff and repository context. Return ONLY valid JSON with this schema:
{"verdict":"approve|request_changes|comment","summary":"...","findings":[{"message":"...","path":"optional","line":0,"side":"RIGHT|LEFT","severity":"info|warning|error"}],"checks":[{"name":"...","status":"pass|fail|skip"}]}
Use inline locations only when they are on changed files. Do not invent findings."""


@dataclass
class ReviewApplication:
    input_source: ReviewInputSource
    executor: ReviewExecutor
    workspace_manager: WorkspaceManager
    destination: ReviewDestination

    def poll_once(self) -> list[ReviewEvent]:
        processed: list[ReviewEvent] = []
        for event in self.input_source.poll():
            workspace_result = None
            number = event.provider_state.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                print(f"[review] {event.repository}: missing PR number", flush=True)
                continue
            task_id = f"review:{event.repository}#{number}"
            try:
                workspace_result = self.workspace_manager.prepare(WorkspaceRequest(
                    task_id, event.repository, event.provider_state.get("head_ref", ""),
                    event.provider_state.get("base_ref", ""), event.provider_state,
                ))
                request = ReviewRequest(task_id, event.repository,
                                        workspace_result.workspace, REVIEW_PROMPT, event.provider_state)
                result = self.executor.execute(request)
                if not result.success:
                    raise RuntimeError(result.summary or "review execution failed")
                self.destination.publish(request, result)
                processed.append(event)
            except Exception as exc:
                print(f"[review] {event.repository}#{number}: {exc}", flush=True)
            finally:
                if workspace_result is not None:
                    try:
                        self.workspace_manager.cleanup(workspace_result)
                    except Exception as exc:
                        print(f"[review] cleanup {event.repository}#{number}: {exc}", flush=True)
        return processed


def compose_review_runtime(store: Any | None = None) -> ReviewApplication:
    pipeline = config.load_review_pipeline_config()
    from orchestrator.providers import (
        REVIEW_DESTINATION_PROVIDERS, REVIEW_EXECUTOR_PROVIDERS,
        REVIEW_INPUT_PROVIDERS, REVIEW_WORKSPACE_PROVIDERS,
    )
    def create(registry, provider):
        options = {**provider.options, "_runtime": True}
        if store is not None:
            options["store"] = store
        return registry.create(provider.type, options)
    return ReviewApplication(
        create(REVIEW_INPUT_PROVIDERS, pipeline.input_source),
        create(REVIEW_EXECUTOR_PROVIDERS, pipeline.executor),
        create(REVIEW_WORKSPACE_PROVIDERS, pipeline.workspace_manager),
        create(REVIEW_DESTINATION_PROVIDERS, pipeline.destination),
    )
