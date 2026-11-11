"""Provider-neutral step vocabulary for configurable stage workflows.

Value model and validation rules shared by configuration parsing (composition
root) and workflow graph builders. Module-level catalogs declare which step
types each stage accepts, :func:`default_steps` reproduces today's hardcoded
sequences, and :func:`validate_steps` enforces the configuration contract.
No YAML parsing or I/O happens here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class StepConfigError(Exception):
    """A workflow step configuration is invalid."""


@dataclass(frozen=True)
class StepSpec:
    """One declarative step of a stage workflow.

    ``on_failure`` is ``"abort"`` (default: route to END) or ``"continue"``
    (record the failure and keep going). ``command`` and ``timeout_seconds``
    only apply to ``command`` steps; ``agent`` only applies to the generic
    ``agent`` step type, where ``None`` means the stage default agent name.
    ``prompt_file`` is a path relative to the orchestrator repo root, resolved
    and loaded later by composition (required for the generic ``agent`` step
    type).
    """

    name: str
    type: str
    on_failure: str = "abort"
    executor_type: str | None = None
    model_name: str | None = None
    model_variant: str | None = None
    prompt_file: str | None = None
    command: str | None = None
    timeout_seconds: int | None = None
    agent: str | None = None


EXECUTION_STEP_TYPES = ("checkout", "plan", "implement", "publish", "cleanup", "command", "agent")
COMMENT_IMPL_STEP_TYPES = ("checkout", "implement_comment", "publish", "cleanup", "command", "agent")
DISCUSS_STEP_TYPES = ("checkout", "discuss", "publish")
REVIEW_STEP_TYPES = ("checkout", "review", "publish", "command")
TRIAGE_STEP_TYPES = ("assess", "publish")

AGENT_TYPES = ("plan", "implement", "implement_comment", "review", "assess", "discuss", "agent")

_STEP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ON_FAILURE_VALUES = ("abort", "continue")
_AGENT_OVERRIDE_FIELDS = ("executor_type", "model_name", "model_variant", "prompt_file", "agent")

_DEFAULT_STEP_SEQUENCES: dict[str, tuple[StepSpec, ...]] = {
    "execution": (
        StepSpec("prepare_workspace", "checkout"),
        StepSpec("plan", "plan"),
        StepSpec("implement", "implement"),
        StepSpec("create_pr", "publish"),
        StepSpec("cleanup", "cleanup", on_failure="continue"),
    ),
    "comment_impl": (
        StepSpec("checkout", "checkout"),
        StepSpec("implement", "implement_comment"),
        StepSpec("publish", "publish"),
        StepSpec("cleanup", "cleanup", on_failure="continue"),
    ),
    "discuss": (
        StepSpec("checkout", "checkout"),
        StepSpec("answer", "discuss"),
        StepSpec("publish", "publish"),
    ),
    "review": (
        StepSpec("checkout", "checkout"),
        StepSpec("review", "review"),
        StepSpec("publish", "publish"),
    ),
    "triage": (
        StepSpec("assess", "assess"),
        StepSpec("publish", "publish"),
    ),
}


def default_steps(stage: str) -> tuple[StepSpec, ...]:
    """Return the built-in step sequence for a stage.

    Sequences reproduce today's behavior exactly, with today's node names kept
    stable. ``cleanup`` steps use ``on_failure: "continue"`` wherever a cleanup
    failure is logged but never fails the run.
    """
    try:
        return _DEFAULT_STEP_SEQUENCES[stage]
    except KeyError:
        known = ", ".join(sorted(_DEFAULT_STEP_SEQUENCES))
        raise StepConfigError(f"unknown stage {stage!r} (known stages: {known})") from None


def validate_steps(
    steps: list[StepSpec] | tuple[StepSpec, ...],
    allowed_types: tuple[str, ...],
    stage: str = "workflow",
) -> None:
    """Validate a stage workflow against the stage's step-type catalog.

    Raises :class:`StepConfigError` with an actionable message naming the step
    and the offending field. Plain dicts are rejected: parsing belongs to the
    composition root; only :class:`StepSpec` instances are accepted here.
    """
    if not isinstance(steps, (list, tuple)):
        raise StepConfigError(
            f"{stage} workflow steps must be a list of StepSpec instances, "
            f"got {type(steps).__name__} (parse YAML step dicts into StepSpec first)"
        )
    if not steps:
        raise StepConfigError(f"{stage} workflow must define at least one step")
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, StepSpec):
            raise StepConfigError(
                f"{stage} workflow steps must be StepSpec instances, got {type(step).__name__}"
            )
        if not isinstance(step.name, str) or not _STEP_NAME_PATTERN.match(step.name):
            raise StepConfigError(
                f"{stage} step {step.name!r}: invalid step name "
                f"(must match '{_STEP_NAME_PATTERN.pattern}')"
            )
        if step.name == "end":
            raise StepConfigError(
                f"{stage} step 'end': step name 'end' is reserved for the workflow "
                "router; choose another name"
            )
        if step.name in seen:
            raise StepConfigError(
                f"{stage} step '{step.name}': duplicate step name (names must be unique)"
            )
        seen.add(step.name)
        if step.type not in allowed_types:
            raise StepConfigError(
                f"{stage} step '{step.name}': unknown step type "
                f"'{step.type}' (allowed: {', '.join(allowed_types)})"
            )
        if step.on_failure not in _ON_FAILURE_VALUES:
            raise StepConfigError(
                f"{stage} step '{step.name}': invalid 'on_failure' {step.on_failure!r} "
                f"(must be one of {', '.join(_ON_FAILURE_VALUES)})"
            )
        if step.type == "publish" and step.on_failure == "continue":
            raise StepConfigError(
                f"{stage} step '{step.name}': 'publish' steps cannot use "
                "'on_failure: continue' (publication failures must abort)"
            )
        if step.type == "command":
            if not isinstance(step.command, str) or not step.command.strip():
                raise StepConfigError(
                    f"{stage} step '{step.name}': 'command' is required for command steps"
                )
            timeout = step.timeout_seconds
            if timeout is not None and (
                isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
            ):
                raise StepConfigError(
                    f"{stage} step '{step.name}': 'timeout_seconds' must be a positive "
                    f"integer, got {timeout!r}"
                )
        elif step.command is not None or step.timeout_seconds is not None:
            raise StepConfigError(
                f"{stage} step '{step.name}': 'command'/'timeout_seconds' are only "
                "valid on 'command' steps"
            )
        if step.type == "agent" and (
            not isinstance(step.prompt_file, str) or not step.prompt_file.strip()
        ):
            raise StepConfigError(
                f"{stage} step '{step.name}': 'prompt_file' is required for agent steps"
            )
        if step.type not in AGENT_TYPES:
            for field in _AGENT_OVERRIDE_FIELDS:
                if getattr(step, field) is not None:
                    raise StepConfigError(
                        f"{stage} step '{step.name}': '{field}' is only valid on "
                        f"agent-bearing step types (one of {', '.join(AGENT_TYPES)})"
                    )
        if step.agent is not None and step.type != "agent":
            raise StepConfigError(
                f"{stage} step '{step.name}': 'agent' is only valid on the "
                "generic 'agent' step type"
            )
