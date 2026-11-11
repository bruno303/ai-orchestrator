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


def _load_step_prompts(steps: list[StepSpec], *, stage: str) -> dict[str, str]:
    """Load every configured prompt file once, failing fast on missing files."""
    prompts: dict[str, str] = {}
    for step in steps:
        if not step.prompt_file:
            continue
        path = config.REPO_ROOT / step.prompt_file
        try:
            prompts[step.name] = path.read_text()
        except OSError as exc:
            raise ValueError(
                f"{stage} step '{step.name}': cannot read prompt file {path}: {exc}"
            ) from exc
    return prompts


def _compose_execution_steps(
    steps: list[StepSpec],
    *,
    runtime: ExecutionRuntime,
    stage: str,
    reuse_workspace: bool,
) -> tuple[list[StepSpec], "ExecutionStageDeps"]:
    from orchestrator.infra.langgraph.execution_steps import ExecutionStageDeps

    pipeline = config.load_pipeline_config().execution
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
    deps = ExecutionStageDeps(
        runtime=runtime,
        step_runtimes=step_runtimes,
        prompts=_load_step_prompts(steps, stage=stage),
        command_timeout_seconds=config.COMMAND_TIMEOUT_SECONDS,
        reuse_workspace=reuse_workspace,
    )
    return steps, deps


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
    pipeline = config.load_pipeline_config().execution
    if runtime is None:
        runtime = compose_execution_runtime()
    return _compose_execution_steps(
        list(pipeline.workflow), runtime=runtime, stage="execution", reuse_workspace=False,
    )


def compose_comment_impl_workflow(
    *, runtime: ExecutionRuntime | None = None,
) -> tuple[list[StepSpec], "ExecutionStageDeps"]:
    """Compose the ``/ai-agent-impl`` steps with workspace reuse enabled."""
    pipeline = config.load_pipeline_config().execution
    if runtime is None:
        runtime = compose_execution_runtime()
    return _compose_execution_steps(
        list(pipeline.comment_impl_workflow),
        runtime=runtime,
        stage="comment_impl",
        reuse_workspace=True,
    )


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


def compose_discussion_workflow(
    *, runtime: DiscussionRuntime | None = None,
) -> tuple[list[StepSpec], "DiscussionStageDeps"]:
    """Compose the ``/ai-agent-discuss`` steps and their per-step overrides."""
    from orchestrator.infra.langgraph.discussion_steps import DiscussionStageDeps

    pipeline = config.load_pipeline_config().execution
    steps = list(pipeline.comment_discuss_workflow)
    if runtime is None:
        runtime = compose_discussion_runtime()
    stage_model = config.load_execution_model_config()
    step_runtimes: dict[str, DiscussionRuntime] = {}
    for step in steps:
        if step.executor_type is None and step.model_name is None and step.model_variant is None:
            continue
        model_config = (
            config.ModelConfig(step.model_name, step.model_variant)
            if step.model_name is not None or step.model_variant is not None
            else stage_model
        )
        executor = _create(
            DISCUSSION_EXECUTOR_PROVIDERS,
            config.ProviderConfig(
                type=step.executor_type or pipeline.executor.type,
                options=dict(pipeline.executor.options),
            ),
            overrides={"model_config": model_config},
        )
        step_runtimes[step.name] = DiscussionRuntime(
            executor,
            runtime.workspace_manager,
            runtime.destination,
            repository_allowed=runtime.repository_allowed,
            model_config=model_config,
            task_log_path=runtime.task_log_path,
        )
    deps = DiscussionStageDeps(
        runtime=runtime,
        step_runtimes=step_runtimes,
        prompts=_load_step_prompts(steps, stage="discuss"),
    )
    return steps, deps


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


def compose_review_workflow(
    *, runtime: ReviewRuntime, pipeline=None,
) -> tuple[list[StepSpec], "ReviewStageDeps"]:
    """Compose the review steps and their per-step runtime/prompt overrides."""
    from orchestrator.infra.filesystem.command import run_workspace_command
    from orchestrator.infra.langgraph.review_steps import ReviewStageDeps

    pipeline = pipeline or config.load_review_pipeline_config()
    steps = list(pipeline.workflow)
    stage_model = config.load_review_model_config()
    step_runtimes: dict[str, ReviewRuntime] = {}
    for step in steps:
        if step.executor_type is None and step.model_name is None and step.model_variant is None:
            continue
        model_config = (
            config.ModelConfig(step.model_name, step.model_variant)
            if step.model_name is not None or step.model_variant is not None
            else stage_model
        )
        executor = _create(
            REVIEW_EXECUTOR_PROVIDERS,
            config.ProviderConfig(
                type=step.executor_type or pipeline.executor.type,
                options=dict(pipeline.executor.options),
            ),
            overrides={"model_config": model_config},
        )
        step_runtimes[step.name] = ReviewRuntime(
            executor,
            runtime.workspace_manager,
            runtime.destination,
            task_log_path=runtime.task_log_path,
        )
    deps = ReviewStageDeps(
        runtime=runtime,
        step_runtimes=step_runtimes,
        prompts=_load_step_prompts(steps, stage="review"),
        command_runner=run_workspace_command,
        command_timeout_seconds=config.COMMAND_TIMEOUT_SECONDS,
    )
    return steps, deps


def compose_review_runtime() -> ReviewApplication:
    from orchestrator.infra.filesystem import workspace
    from orchestrator.infra.langgraph import review_steps
    from orchestrator.infra.langgraph.runner import run_workflow_graph
    from orchestrator.infra.langgraph.state import ReviewState

    pipeline = config.load_review_pipeline_config()
    source = _create(
        REVIEW_INPUT_PROVIDERS,
        pipeline.input_source,
        overrides=_label_filter_options(pipeline.labels),
    )
    runtime = ReviewRuntime(
        _create(REVIEW_EXECUTOR_PROVIDERS, pipeline.executor),
        _create(REVIEW_WORKSPACE_PROVIDERS, pipeline.workspace_manager),
        _create(
            REVIEW_DESTINATION_PROVIDERS,
            pipeline.destination,
            overrides=_label_output_options(pipeline.labels.output),
        ),
        task_log_path=workspace.task_log_path,
    )
    steps, deps = compose_review_workflow(runtime=runtime, pipeline=pipeline)

    def run_workflow(target):
        return run_workflow_graph(
            review_steps.seed(target), target.id,
            steps=steps, deps=deps, factories=review_steps.STEP_FACTORIES,
            state_schema=ReviewState,
        )

    return ReviewApplication(
        source,
        runtime=runtime,
        write_task_log=workspace.write_task_log,
        run_workflow=run_workflow,
    )


def compose_triage_workflow(
    *, executor, destination, pipeline=None,
) -> tuple[list[StepSpec], "TriageStageDeps"]:
    """Compose the triage steps and their per-step executor/prompt overrides."""
    from orchestrator.infra.filesystem import workspace
    from orchestrator.infra.langgraph.triage_steps import TriageStageDeps

    pipeline = pipeline or config.load_triage_pipeline_config()
    steps = list(pipeline.workflow)
    stage_model = config.load_triage_model_config()
    step_executors = {}
    for step in steps:
        if step.executor_type is None and step.model_name is None and step.model_variant is None:
            continue
        model_config = (
            config.ModelConfig(step.model_name, step.model_variant)
            if step.model_name is not None or step.model_variant is not None
            else stage_model
        )
        step_executors[step.name] = _create(
            TRIAGE_EXECUTOR_PROVIDERS,
            config.ProviderConfig(
                type=step.executor_type or pipeline.executor.type,
                options=dict(pipeline.executor.options),
            ),
            overrides={"model_config": model_config},
        )
    deps = TriageStageDeps(
        executor=executor,
        destination=destination,
        step_executors=step_executors,
        prompts=_load_step_prompts(steps, stage="triage"),
        task_log_path=workspace.task_log_path,
    )
    return steps, deps


def compose_triage_runtime() -> TriageApplication:
    from orchestrator.infra.filesystem import workspace
    from orchestrator.infra.langgraph import triage_steps
    from orchestrator.infra.langgraph.runner import run_workflow_graph
    from orchestrator.infra.langgraph.state import TriageState

    pipeline = config.load_triage_pipeline_config()
    source = _create(
        TRIAGE_INPUT_PROVIDERS,
        pipeline.input_source,
        overrides=_label_filter_options(pipeline.labels),
    )
    executor = _create(TRIAGE_EXECUTOR_PROVIDERS, pipeline.executor)
    triage_output = pipeline.labels.output
    destination = _create(
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
    )
    steps, deps = compose_triage_workflow(
        executor=executor, destination=destination, pipeline=pipeline,
    )

    def run_workflow(target):
        return run_workflow_graph(
            triage_steps.seed(target), target.id,
            steps=steps, deps=deps, factories=triage_steps.STEP_FACTORIES,
            state_schema=TriageState,
        )

    return TriageApplication(
        source,
        executor,
        destination,
        context_presenter=getattr(source, "context_presenter", NoopContextPresenter()),
        task_log_path=workspace.task_log_path,
        write_task_log=workspace.write_task_log,
        run_workflow=run_workflow,
    )
