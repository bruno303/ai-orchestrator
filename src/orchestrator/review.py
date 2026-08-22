"""Provider-neutral pull-request review workflow adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator import config, workspace
from orchestrator.domain import ContextPresenter, NoopContextPresenter, ReviewTarget
from orchestrator.providers import ReviewDestination, ReviewExecutor, ReviewInputSource, WorkspaceManager
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
    context_presenter: ContextPresenter

    def __init__(
        self,
        input_source: ReviewInputSource,
        executor: ReviewExecutor | None = None,
        workspace_manager: WorkspaceManager | None = None,
        destination: ReviewDestination | None = None,
        runtime: ReviewRuntime | None = None,
        context_presenter: ContextPresenter | None = None,
    ) -> None:
        self.input_source = input_source
        self.runtime = runtime or ReviewRuntime(executor, workspace_manager, destination)
        self.context_presenter = context_presenter or getattr(
            input_source, "context_presenter", NoopContextPresenter()
        )

    def poll_once(self) -> list[ReviewTarget]:
        processed: list[ReviewTarget] = []
        for target in self.input_source.poll():
            task_id = target.id
            prepared = None
            fields = dict(self.context_presenter.logging_fields(target.context))
            title = " ".join(target.title.split()) or "<untitled>"
            workspace.write_task_log(
                task_id,
                "review",
                f"[review] starting: repository={target.repository} "
                f"id={target.id} revision={target.revision or '<unknown>'} "
                f"title={title!r} context={fields}",
            )
            print(
                f"[review] starting: repository={target.repository} "
                f"id={target.id} title={title!r}",
                flush=True,
            )
            try:
                prepared = self.runtime.prepare(PrepareReviewRequest(target))
                execution = self.runtime.execute_review(ExecuteReviewRequest(prepared, REVIEW_PROMPT))
                self.runtime.publish_review(PublishReviewRequest(prepared, execution))
                processed.append(target)
            except Exception as exc:
                print(f"[review] {target.id}: {exc}", flush=True)
            finally:
                if prepared is not None:
                    try:
                        self.runtime.cleanup_review(CleanupReviewRequest(prepared))
                    except Exception as exc:
                        print(f"[review] cleanup {target.id}: {exc}", flush=True)
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
