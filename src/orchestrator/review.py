"""Provider-neutral pull-request review workflow adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator import config
from orchestrator.providers import ReviewDestination, ReviewEvent, ReviewExecutor, ReviewInputSource, WorkspaceManager
from orchestrator.runtime.models import (
    CleanupReviewRequest,
    ExecuteReviewRequest,
    PrepareReviewRequest,
    PublishReviewRequest,
)
from orchestrator.runtime.review import REVIEW_PROMPT, ReviewRuntime


@dataclass(init=False)
class ReviewApplication:
    """Coordinate review events while delegating concrete steps to a runtime."""

    input_source: ReviewInputSource
    runtime: ReviewRuntime

    def __init__(
        self,
        input_source: ReviewInputSource,
        executor: ReviewExecutor | None = None,
        workspace_manager: WorkspaceManager | None = None,
        destination: ReviewDestination | None = None,
        runtime: ReviewRuntime | None = None,
    ) -> None:
        self.input_source = input_source
        self.runtime = runtime or ReviewRuntime(executor, workspace_manager, destination)

    def poll_once(self) -> list[ReviewEvent]:
        processed: list[ReviewEvent] = []
        for event in self.input_source.poll():
            number = event.provider_state.get("number")
            if not isinstance(number, int) or isinstance(number, bool):
                print(f"[review] {event.repository}: missing PR number", flush=True)
                continue
            task_id = f"review:{event.repository}#{number}"
            prepared = None
            try:
                prepared = self.runtime.prepare(PrepareReviewRequest(event, task_id))
                execution = self.runtime.execute_review(ExecuteReviewRequest(prepared, REVIEW_PROMPT))
                self.runtime.publish_review(PublishReviewRequest(prepared, execution))
                processed.append(event)
            except Exception as exc:
                print(f"[review] {event.repository}#{number}: {exc}", flush=True)
            finally:
                if prepared is not None:
                    try:
                        self.runtime.cleanup_review(CleanupReviewRequest(prepared))
                    except Exception as exc:
                        print(f"[review] cleanup {event.repository}#{number}: {exc}", flush=True)
        return processed


def compose_review_runtime(store: Any | None = None) -> ReviewApplication:
    pipeline = config.load_review_pipeline_config()
    from orchestrator.providers import (
        REVIEW_DESTINATION_PROVIDERS,
        REVIEW_EXECUTOR_PROVIDERS,
        REVIEW_INPUT_PROVIDERS,
        REVIEW_WORKSPACE_PROVIDERS,
    )

    def create(registry, provider):
        options = {**provider.options, "_runtime": True}
        if store is not None:
            options["store"] = store
        return registry.create(provider.type, options)

    return ReviewApplication(
        create(REVIEW_INPUT_PROVIDERS, pipeline.input_source),
        runtime=ReviewRuntime(
            create(REVIEW_EXECUTOR_PROVIDERS, pipeline.executor),
            create(REVIEW_WORKSPACE_PROVIDERS, pipeline.workspace_manager),
            create(REVIEW_DESTINATION_PROVIDERS, pipeline.destination),
        ),
    )
