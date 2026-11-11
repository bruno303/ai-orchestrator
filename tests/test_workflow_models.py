"""Tests for workflow step specs, stage catalogs, built-in defaults, and validation."""

from __future__ import annotations

import dataclasses

import pytest

from orchestrator.application.workflow import (
    AGENT_TYPES,
    COMMENT_IMPL_STEP_TYPES,
    DISCUSS_STEP_TYPES,
    EXECUTION_STEP_TYPES,
    REVIEW_STEP_TYPES,
    TRIAGE_STEP_TYPES,
    StepConfigError,
    StepSpec,
    default_steps,
    validate_steps,
)


EXPECTED_DEFAULT_STEPS: dict[str, list[tuple[str, str, str]]] = {
    "execution": [
        ("prepare_workspace", "checkout", "abort"),
        ("plan", "plan", "abort"),
        ("implement", "implement", "abort"),
        ("create_pr", "publish", "abort"),
        ("cleanup", "cleanup", "continue"),
    ],
    "comment_impl": [
        ("checkout", "checkout", "abort"),
        ("implement", "implement_comment", "abort"),
        ("publish", "publish", "abort"),
        ("cleanup", "cleanup", "continue"),
    ],
    "discuss": [
        ("checkout", "checkout", "abort"),
        ("answer", "discuss", "abort"),
        ("publish", "publish", "abort"),
    ],
    "review": [
        ("checkout", "checkout", "abort"),
        ("review", "review", "abort"),
        ("publish", "publish", "abort"),
    ],
    "triage": [
        ("assess", "assess", "abort"),
        ("publish", "publish", "abort"),
    ],
}

EXPECTED_DEFAULT_STEP_NAMES: dict[str, list[str]] = {
    "execution": ["prepare_workspace", "plan", "implement", "create_pr", "cleanup"],
    "comment_impl": ["checkout", "implement", "publish", "cleanup"],
    "discuss": ["checkout", "answer", "publish"],
    "review": ["checkout", "review", "publish"],
    "triage": ["assess", "publish"],
}

STAGE_STEP_TYPES = {
    "execution": EXECUTION_STEP_TYPES,
    "comment_impl": COMMENT_IMPL_STEP_TYPES,
    "discuss": DISCUSS_STEP_TYPES,
    "review": REVIEW_STEP_TYPES,
    "triage": TRIAGE_STEP_TYPES,
}


def test_stage_step_type_tuples_match_the_plan_catalog():
    assert EXECUTION_STEP_TYPES == ("checkout", "plan", "implement", "publish", "cleanup", "command", "agent")
    assert COMMENT_IMPL_STEP_TYPES == ("checkout", "implement_comment", "publish", "cleanup", "command", "agent")
    assert DISCUSS_STEP_TYPES == ("checkout", "discuss", "publish")
    assert REVIEW_STEP_TYPES == ("checkout", "review", "publish", "command")
    assert TRIAGE_STEP_TYPES == ("assess", "publish")


def test_agent_types_is_the_exact_agent_bearing_catalog():
    assert AGENT_TYPES == ("plan", "implement", "implement_comment", "review", "assess", "discuss", "agent")


@pytest.mark.parametrize("stage", sorted(EXPECTED_DEFAULT_STEPS))
def test_default_steps_reproduce_today_sequences_per_stage(stage: str):
    steps = default_steps(stage)

    assert [(step.name, step.type, step.on_failure) for step in steps] == EXPECTED_DEFAULT_STEPS[stage]


@pytest.mark.parametrize("stage", sorted(EXPECTED_DEFAULT_STEP_NAMES))
def test_default_step_names_are_pinned_per_stage(stage: str):
    assert [step.name for step in default_steps(stage)] == EXPECTED_DEFAULT_STEP_NAMES[stage]


@pytest.mark.parametrize("stage", sorted(STAGE_STEP_TYPES))
def test_default_step_types_are_valid_for_their_stage(stage: str):
    assert all(step.type in STAGE_STEP_TYPES[stage] for step in default_steps(stage))


@pytest.mark.parametrize("stage", sorted(STAGE_STEP_TYPES))
def test_default_steps_pass_validation(stage: str):
    validate_steps(default_steps(stage), STAGE_STEP_TYPES[stage], stage=stage)


def test_default_steps_rejects_unknown_stage():
    with pytest.raises(StepConfigError, match="unknown stage 'bogus'"):
        default_steps("bogus")


def test_step_spec_is_frozen():
    step = StepSpec("implement", "implement")

    with pytest.raises(dataclasses.FrozenInstanceError):
        step.name = "other"


def test_validate_steps_rejects_non_sequence_input():
    with pytest.raises(StepConfigError, match="must be a list of StepSpec"):
        validate_steps({"implement": StepSpec("implement", "implement")}, EXECUTION_STEP_TYPES)


def test_validate_steps_rejects_plain_dict_entries():
    steps = [{"name": "implement", "type": "implement"}]

    with pytest.raises(StepConfigError, match="must be StepSpec instances"):
        validate_steps(steps, EXECUTION_STEP_TYPES)


def test_validate_steps_rejects_empty_step_lists():
    with pytest.raises(StepConfigError, match="at least one step"):
        validate_steps([], EXECUTION_STEP_TYPES)


@pytest.mark.parametrize("name", ["Plan", "1plan", "", "a" * 41, "has space"])
def test_step_names_must_be_lowercase_slugs(name: str):
    with pytest.raises(StepConfigError, match="invalid step name"):
        validate_steps([StepSpec(name, "publish")], EXECUTION_STEP_TYPES, stage="execution")


def test_step_name_of_maximum_length_is_accepted():
    validate_steps([StepSpec("a" * 40, "publish")], EXECUTION_STEP_TYPES, stage="execution")


def test_duplicate_step_names_are_rejected():
    steps = [StepSpec("implement", "implement"), StepSpec("implement", "publish")]

    with pytest.raises(StepConfigError, match="duplicate step name"):
        validate_steps(steps, EXECUTION_STEP_TYPES, stage="execution")


@pytest.mark.parametrize("stage", sorted(STAGE_STEP_TYPES))
def test_step_named_end_is_reserved_for_the_workflow_router(stage: str):
    steps = [*default_steps(stage), StepSpec("end", "publish")]

    with pytest.raises(StepConfigError, match="'end' is reserved for the workflow router"):
        validate_steps(steps, STAGE_STEP_TYPES[stage], stage=stage)


def test_unknown_step_type_is_rejected_against_the_stage_catalog():
    with pytest.raises(StepConfigError, match="triage step 'implement': unknown step type 'implement'"):
        validate_steps([StepSpec("implement", "implement")], TRIAGE_STEP_TYPES, stage="triage")


def test_unknown_step_type_is_rejected_against_an_empty_catalog():
    with pytest.raises(StepConfigError, match="unknown step type 'checkout'"):
        validate_steps([StepSpec("checkout", "checkout")], ())


def test_invalid_on_failure_value_is_rejected():
    step = StepSpec("implement", "implement", on_failure="retry")

    with pytest.raises(StepConfigError, match="invalid 'on_failure' 'retry'"):
        validate_steps([step], EXECUTION_STEP_TYPES, stage="execution")


def test_command_step_without_a_command_is_rejected():
    with pytest.raises(
        StepConfigError,
        match="execution step 'tests': 'command' is required for command steps",
    ):
        validate_steps([StepSpec("tests", "command")], EXECUTION_STEP_TYPES, stage="execution")


def test_command_step_with_a_blank_command_is_rejected():
    step = StepSpec("tests", "command", command="   ")

    with pytest.raises(StepConfigError, match="'command' is required for command steps"):
        validate_steps([step], EXECUTION_STEP_TYPES, stage="execution")


@pytest.mark.parametrize(("field", "value"), [("command", "pytest -q"), ("timeout_seconds", 60)])
def test_command_fields_are_rejected_on_non_command_steps(field: str, value: object):
    step = StepSpec("implement", "implement", **{field: value})

    with pytest.raises(StepConfigError, match=f"execution step 'implement': .+'{field}'"):
        validate_steps([step], EXECUTION_STEP_TYPES, stage="execution")


@pytest.mark.parametrize("timeout", [0, -5, "600", True])
def test_timeout_seconds_must_be_a_positive_int(timeout: object):
    step = StepSpec("tests", "command", command="pytest -q", timeout_seconds=timeout)

    with pytest.raises(StepConfigError, match="'timeout_seconds' must be a positive integer"):
        validate_steps([step], EXECUTION_STEP_TYPES, stage="execution")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executor_type", "claude"),
        ("model_name", "verboo/deepseek-v4-flash"),
        ("model_variant", "flash"),
        ("prompt_file", "prompts/plan.md"),
        ("agent", "writer"),
    ],
)
def test_agent_overrides_are_rejected_on_non_agent_step_types(field: str, value: str):
    step = StepSpec("create_pr", "publish", **{field: value})

    with pytest.raises(StepConfigError, match=f"execution step 'create_pr': '{field}' is only valid"):
        validate_steps([step], EXECUTION_STEP_TYPES, stage="execution")


def test_agent_name_is_rejected_on_fixed_agent_bearing_steps():
    step = StepSpec("plan", "plan", agent="writer")

    with pytest.raises(
        StepConfigError,
        match="execution step 'plan': 'agent' is only valid on the generic 'agent' step type",
    ):
        validate_steps([step], EXECUTION_STEP_TYPES, stage="execution")


def test_agent_steps_require_a_prompt_file():
    with pytest.raises(
        StepConfigError,
        match="execution step 'document': 'prompt_file' is required for agent steps",
    ):
        validate_steps([StepSpec("document", "agent")], EXECUTION_STEP_TYPES, stage="execution")


def test_agent_steps_with_a_prompt_file_pass():
    validate_steps(
        [StepSpec("document", "agent", prompt_file="prompts/document.md")],
        EXECUTION_STEP_TYPES,
        stage="execution",
    )


def test_valid_reordered_execution_subset_without_planning_passes():
    steps = [
        StepSpec("implement", "implement", executor_type="claude", model_name="verboo/model"),
        StepSpec("prepare_workspace", "checkout"),
        StepSpec("create_pr", "publish"),
    ]

    validate_steps(steps, EXECUTION_STEP_TYPES, stage="execution")


@pytest.mark.parametrize("stage", sorted(STAGE_STEP_TYPES))
def test_publish_steps_cannot_continue_after_failure(stage: str):
    steps = [StepSpec("publish", "publish", on_failure="continue")]

    with pytest.raises(
        StepConfigError,
        match=f"{stage} step 'publish': 'publish' steps cannot use 'on_failure: continue'",
    ):
        validate_steps(steps, STAGE_STEP_TYPES[stage], stage=stage)


def test_valid_command_step_with_every_field_set_passes():
    steps = [StepSpec("tests", "command", command="pytest -q", timeout_seconds=900)]

    validate_steps(steps, EXECUTION_STEP_TYPES, stage="execution")


def test_valid_generic_agent_step_with_all_overrides_passes():
    steps = [
        StepSpec(
            "document", "agent", on_failure="continue",
            executor_type="codex", model_name="verboo/model", model_variant="mini",
            prompt_file="prompts/document.md", agent="writer",
        ),
        StepSpec("publish", "publish"),
    ]

    validate_steps(steps, EXECUTION_STEP_TYPES, stage="execution")
