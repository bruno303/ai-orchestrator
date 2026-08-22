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
from orchestrator.main import config
from orchestrator.main.providers import (
    DESTINATION_PROVIDERS,
    EXECUTOR_PROVIDERS,
    INPUT_PROVIDERS,
    REVIEW_DESTINATION_PROVIDERS,
    REVIEW_EXECUTOR_PROVIDERS,
    REVIEW_INPUT_PROVIDERS,
    REVIEW_WORKSPACE_PROVIDERS,
    WORKSPACE_PROVIDERS,
)


def _create(registry, provider):
    settings = {}
    if registry in (INPUT_PROVIDERS, REVIEW_INPUT_PROVIDERS):
        settings["_config_module"] = config
    if registry is REVIEW_EXECUTOR_PROVIDERS:
        settings["model_config"] = config.load_review_model_config()
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


def compose_execution_runtime(*, executor=None, workspace_manager=None, destination=None) -> ExecutionRuntime:
    from orchestrator.infra.filesystem import workspace

    pipeline = config.load_pipeline_config().execution
    return ExecutionRuntime(
        executor or _create(EXECUTOR_PROVIDERS, pipeline.executor),
        workspace_manager or _create(WORKSPACE_PROVIDERS, pipeline.workspace_manager),
        destination or _create(DESTINATION_PROVIDERS, pipeline.destination),
        repository_allowed=config.is_repository_allowed,
        agent_settings=_agent_settings(),
        task_log_path=workspace.task_log_path,
    )


def compose_runtime() -> Runtime:
    pipeline = config.load_pipeline_config().execution
    source = _create(INPUT_PROVIDERS, pipeline.input_source)
    executor = _create(EXECUTOR_PROVIDERS, pipeline.executor)
    manager = _create(WORKSPACE_PROVIDERS, pipeline.workspace_manager)
    destination = _create(DESTINATION_PROVIDERS, pipeline.destination)
    return Runtime(
        source,
        executor,
        manager,
        destination,
        getattr(source, "feedback", None),
        pipeline.input_source.type,
        compose_execution_runtime(
            executor=executor, workspace_manager=manager, destination=destination
        ),
        getattr(source, "context_presenter", NoopContextPresenter()),
    )


def compose_review_runtime() -> ReviewApplication:
    from orchestrator.infra.filesystem import workspace

    pipeline = config.load_review_pipeline_config()
    source = _create(REVIEW_INPUT_PROVIDERS, pipeline.input_source)
    return ReviewApplication(
        source,
        runtime=ReviewRuntime(
            _create(REVIEW_EXECUTOR_PROVIDERS, pipeline.executor),
            _create(REVIEW_WORKSPACE_PROVIDERS, pipeline.workspace_manager),
            _create(REVIEW_DESTINATION_PROVIDERS, pipeline.destination),
            task_log_path=workspace.task_log_path,
        ),
        write_task_log=workspace.write_task_log,
    )
