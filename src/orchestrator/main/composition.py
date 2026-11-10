"""Production application composition.

Only this module wires concrete infrastructure adapters to application services.
"""

from __future__ import annotations

from orchestrator.application.execution.agent import AgentSettings
from orchestrator.application.execution.service import ExecutionRuntime
from orchestrator.application.discussion import DiscussionRuntime
from orchestrator.application.polling import Runtime
from orchestrator.application.ports import NoopContextPresenter
from orchestrator.application.workflow import StepSpec
from orchestrator.application.review import ReviewApplication
from orchestrator.application.review.service import ReviewRuntime
from orchestrator.application.triage import TriageApplication
from orchestrator.main import config
from orchestrator.main.providers import (
    DESTINATION_PROVIDERS,
    DISCUSSION_DESTINATION_PROVIDERS,
    DISCUSSION_EXECUTOR_PROVIDERS,
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


def compose_execution_runtime(*, executor=None, workspace_manager=None, destination=None,
                              agent_settings: AgentSettings | None = None) -> ExecutionRuntime:
    from orchestrator.infra.filesystem import workspace
    from orchestrator.infra.filesystem.command import run_workspace_command

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
        agent_settings=agent_settings if agent_settings is not None else _agent_settings(),
        task_log_path=workspace.task_log_path,
        command_runner=run_workspace_command,
    )


def compose_execution_workflow(*, runtime: ExecutionRuntime | None = None) -> tuple[list[StepSpec], "ExecutionStageDeps"]:
    """Compose the configured execution stage steps and their shared deps.

    Returns the configured (or built-in default) step list plus an
    :class:`ExecutionStageDeps` bundle: the default (or caller-provided)
    runtime, dedicated per-step runtimes for steps that override executor/model
    (sharing that runtime's workspace manager and destination), prompt-file
    contents loaded once and failing fast on missing files, and the configured
    command-step timeout. Passing a prebuilt ``runtime`` skips re-composing the
    default one; all composition errors surface here, before any graph runs.
    """
    from orchestrator.infra.langgraph.execution_steps import ExecutionStageDeps

    pipeline = config.load_pipeline_config().execution
    steps = list(pipeline.workflow)
    if runtime is None:
        runtime = compose_execution_runtime()
    step_runtimes: dict[str, ExecutionRuntime] = {}
    for step in steps:
        if step.executor_type is None and step.model_name is None and step.model_variant is None:
            continue  # steps without overrides reuse the default runtime
        if step.executor_type is not None:
            # Same option resolution the pipeline executor gets (mirroring
            # _create(EXECUTOR_PROVIDERS, pipeline.executor)), so auth-ish
            # settings configured for the stage carry over.
            executor = _create(
                EXECUTOR_PROVIDERS,
                config.ProviderConfig(type=step.executor_type, options=dict(pipeline.executor.options)),
            )
        else:
            executor = runtime.agent.executor
        agent_settings = None
        if step.model_name is not None or step.model_variant is not None:
            agent_settings = AgentSettings(config.ModelConfig(step.model_name, step.model_variant))
        step_runtimes[step.name] = compose_execution_runtime(
            executor=executor,
            workspace_manager=runtime.workspace_manager,
            destination=runtime.destination,
            agent_settings=agent_settings,
        )
    prompts: dict[str, str] = {}
    for step in steps:
        if not step.prompt_file:
            continue
        path = config.REPO_ROOT / step.prompt_file
        try:
            prompts[step.name] = path.read_text()
        except OSError as exc:
            raise ValueError(
                f"execution step '{step.name}': cannot read prompt file {path}: {exc}"
            ) from exc
    deps = ExecutionStageDeps(
        runtime=runtime,
        step_runtimes=step_runtimes,
        prompts=prompts,
        command_timeout_seconds=config.COMMAND_TIMEOUT_SECONDS,
        reuse_workspace=False,
    )
    return steps, deps


def compose_discussion_runtime() -> DiscussionRuntime:
    from orchestrator.infra.filesystem import workspace

    pipeline = config.load_pipeline_config().execution
    return DiscussionRuntime(
        _create(
            DISCUSSION_EXECUTOR_PROVIDERS,
            pipeline.executor,
            overrides={"model_config": config.load_execution_model_config()},
        ),
        _create(WORKSPACE_PROVIDERS, pipeline.workspace_manager),
        _create(DISCUSSION_DESTINATION_PROVIDERS, pipeline.destination),
        repository_allowed=config.is_repository_allowed,
        model_config=config.load_execution_model_config(),
        task_log_path=workspace.task_log_path,
    )


def compose_runtime() -> Runtime:
    pipeline = config.load_pipeline_config().execution
    source = _create(
        INPUT_PROVIDERS,
        pipeline.input_source,
        overrides={
            **_label_filter_options(pipeline.labels),
            "verbosity": config.load_logging_config().verbosity,
        },
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
    discussion_runtime = compose_discussion_runtime()
    return Runtime(
        source,
        executor,
        manager,
        destination,
        getattr(source, "feedback", None),
        pipeline.input_source.type,
        execution_runtime,
        getattr(source, "context_presenter", NoopContextPresenter()),
        discussion_runtime,
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
