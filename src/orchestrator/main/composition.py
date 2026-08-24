"""Production application composition.

Only this module wires concrete infrastructure adapters to application services.
"""

from __future__ import annotations

from orchestrator.application.execution.agent import AgentSettings
from orchestrator.application.execution.service import ExecutionRuntime
from orchestrator.application.polling import Runtime
from orchestrator.application.ports import NoopContextPresenter
from orchestrator.application.review import ReviewApplication
from orchestrator.application.review.service import ReviewRuntime
from orchestrator.application.triage import TriageApplication
from orchestrator.main import config
from orchestrator.main.providers import (
    DESTINATION_PROVIDERS,
    EXECUTOR_PROVIDERS,
    INPUT_PROVIDERS,
    REVIEW_DESTINATION_PROVIDERS,
    REVIEW_EXECUTOR_PROVIDERS,
    REVIEW_INPUT_PROVIDERS,
    REVIEW_WORKSPACE_PROVIDERS,
    TRIAGE_DESTINATION_PROVIDERS,
    TRIAGE_EXECUTOR_PROVIDERS,
    TRIAGE_INPUT_PROVIDERS,
    WORKSPACE_PROVIDERS,
)


def _create(registry, provider, *, overrides: dict | None = None):
    settings = {}
    if registry in (INPUT_PROVIDERS, REVIEW_INPUT_PROVIDERS, TRIAGE_INPUT_PROVIDERS):
        settings["_config_module"] = config
    if registry is REVIEW_EXECUTOR_PROVIDERS:
        settings["model_config"] = config.load_review_model_config()
    if registry is TRIAGE_EXECUTOR_PROVIDERS:
        settings["model_config"] = config.load_triage_model_config()
    settings.update(overrides or {})
    return registry.create(
        provider.type,
        {
            **provider.options,
            "_runtime": True,
            **settings,
        },
    )


def _agent_settings() -> AgentSettings:
    return AgentSettings(config.load_execution_model_config())


def _label_filter_options(contract) -> dict:
    return {
        "select_labels": list(contract.select),
        "suppress_labels": list(contract.suppress),
    }


def _label_output_options(output) -> dict:
    return {
        "output_labels": list(output.add),
        "remove_output_labels": list(output.remove),
    }


def compose_execution_runtime(*, executor=None, workspace_manager=None, destination=None) -> ExecutionRuntime:
    from orchestrator.infra.filesystem import workspace

    pipeline = config.load_pipeline_config().execution
    manager = workspace_manager or _create(WORKSPACE_PROVIDERS, pipeline.workspace_manager)
    publication = destination or _create(
        DESTINATION_PROVIDERS,
        pipeline.destination,
        overrides=_label_output_options(pipeline.labels.output),
    )
    if hasattr(publication, "git_client") and hasattr(manager, "git_client"):
        publication.git_client = manager.git_client
    return ExecutionRuntime(
        executor or _create(EXECUTOR_PROVIDERS, pipeline.executor),
        manager,
        publication,
        repository_allowed=config.is_repository_allowed,
        agent_settings=_agent_settings(),
        task_log_path=workspace.task_log_path,
    )


def compose_runtime() -> Runtime:
    pipeline = config.load_pipeline_config().execution
    source = _create(
        INPUT_PROVIDERS,
        pipeline.input_source,
        overrides=_label_filter_options(pipeline.labels),
    )
    executor = _create(EXECUTOR_PROVIDERS, pipeline.executor)
    manager = _create(WORKSPACE_PROVIDERS, pipeline.workspace_manager)
    destination = _create(
        DESTINATION_PROVIDERS,
        pipeline.destination,
        overrides=_label_output_options(pipeline.labels.output),
    )
    execution_runtime = compose_execution_runtime(
        executor=executor, workspace_manager=manager, destination=destination
    )
    return Runtime(
        source,
        executor,
        manager,
        destination,
        getattr(source, "feedback", None),
        pipeline.input_source.type,
        execution_runtime,
        getattr(source, "context_presenter", NoopContextPresenter()),
    )


def compose_review_runtime() -> ReviewApplication:
    from orchestrator.infra.filesystem import workspace

    pipeline = config.load_review_pipeline_config()
    source = _create(
        REVIEW_INPUT_PROVIDERS,
        pipeline.input_source,
        overrides=_label_filter_options(pipeline.labels),
    )
    return ReviewApplication(
        source,
        runtime=ReviewRuntime(
            _create(REVIEW_EXECUTOR_PROVIDERS, pipeline.executor),
            _create(REVIEW_WORKSPACE_PROVIDERS, pipeline.workspace_manager),
            _create(
                REVIEW_DESTINATION_PROVIDERS,
                pipeline.destination,
                overrides=_label_output_options(pipeline.labels.output),
            ),
            task_log_path=workspace.task_log_path,
        ),
        write_task_log=workspace.write_task_log,
    )


def compose_triage_runtime() -> TriageApplication:
    from orchestrator.infra.filesystem import workspace

    pipeline = config.load_triage_pipeline_config()
    source = _create(
        TRIAGE_INPUT_PROVIDERS,
        pipeline.input_source,
        overrides=_label_filter_options(pipeline.labels),
    )
    triage_output = pipeline.labels.output
    return TriageApplication(
        source,
        _create(TRIAGE_EXECUTOR_PROVIDERS, pipeline.executor),
        _create(
            TRIAGE_DESTINATION_PROVIDERS,
            pipeline.destination,
            overrides={
                "ready_output": {
                    "add": list(triage_output.ready.add),
                    "remove": list(triage_output.ready.remove),
                },
                "blocked_output": {
                    "add": list(triage_output.blocked.add),
                    "remove": list(triage_output.blocked.remove),
                },
            },
        ),
        context_presenter=getattr(source, "context_presenter", NoopContextPresenter()),
        task_log_path=workspace.task_log_path,
        write_task_log=workspace.write_task_log,
    )
