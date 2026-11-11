"""Triage stage catalog: issue assessment and label/comment publication.

Assessment owns its temporary workspace, matching the historical application
behavior; publication receives only the structured outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from orchestrator.application.ports import TriageDestination, TriageExecutor, TriageRequest
from orchestrator.application.triage.service import TRIAGE_PROMPT
from orchestrator.application.workflow import StepSpec
from orchestrator.domain import Context, PublishedTriage, TriageOutcome, TriageTarget
from orchestrator.infra.langgraph.builder import NodeFn
from orchestrator.infra.langgraph.state import COMPLETED, RECEIVED, TriageState

_SNIPPET_LIMIT = 4000


def seed(target: TriageTarget) -> dict[str, Any]:
    """Build the initial triage graph state for one target."""
    return {
        "task_id": target.id,
        "input": {
            "provider": target.input_provider,
            "data": target.to_dict(),
        },
        "processing": {},
        "workspace": {},
        "output": {},
        "status": RECEIVED,
    }


def _target(state: TriageState) -> TriageTarget:
    value = state.get("input") or {}
    data = value.get("data") or {}
    if not data.get("id"):
        raise ValueError("workflow input is missing canonical triage data")
    return TriageTarget(
        id=data["id"],
        repository=data["repository"],
        title=data.get("title", ""),
        description=data.get("description", ""),
        input_provider=data.get("input_provider", value.get("provider", "")),
        context=Context.from_dict(data.get("context") or value.get("context") or {}),
    )


@dataclass(frozen=True)
class TriageStageDeps:
    """Shared triage destination plus per-step executors and prompt templates."""

    executor: TriageExecutor
    destination: TriageDestination
    step_executors: dict[str, TriageExecutor] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    task_log_path: Callable[[str, str], Path] = lambda task_id, node: Path(
        f"{task_id}-{node}.log"
    )

    def executor_for(self, step: StepSpec) -> TriageExecutor:
        return self.step_executors.get(step.name, self.executor)


def _assess_node(step: StepSpec, deps: TriageStageDeps) -> NodeFn:
    def run(state: TriageState) -> dict[str, Any]:
        target = _target(state)
        prompt = deps.prompts.get(step.name) or TRIAGE_PROMPT.format(
            task_id=target.id,
            repository=target.repository,
            title=target.title,
            description=target.description,
        )
        with TemporaryDirectory(prefix="orchestrator-triage-") as directory:
            outcome = deps.executor_for(step).execute(TriageRequest(
                task_id=target.id,
                repository=target.repository,
                workspace=directory,
                prompt=prompt,
                context=target.context,
                log_file=str(deps.task_log_path(target.id, step.name)),
            ))
        if not isinstance(outcome, TriageOutcome):
            raise TypeError("triage executor returned an invalid outcome")
        if not outcome.success:
            raise RuntimeError(outcome.summary or "triage execution failed")
        return {
            "processing": {"outcome": outcome},
            "step_results": {step.name: {"summary": outcome.summary[:_SNIPPET_LIMIT]}},
        }

    return run


def _publish_node(step: StepSpec, deps: TriageStageDeps) -> NodeFn:
    def run(state: TriageState) -> dict[str, Any]:
        outcome = (state.get("processing") or {}).get("outcome")
        if not isinstance(outcome, TriageOutcome):
            raise RuntimeError(
                f"triage step '{step.name}' requires a prior assess step"
            )
        publication = deps.destination.publish(_target(state), outcome)
        output: dict[str, Any] = {}
        if isinstance(publication, PublishedTriage):
            output = {
                "provider": publication.provider,
                "context": publication.context.to_dict(),
            }
            if publication.id is not None:
                output["external_id"] = publication.id
            if publication.url is not None:
                output["url"] = publication.url
        return {"status": COMPLETED, "output": output}

    return run


STEP_FACTORIES: dict[str, Callable[[StepSpec, TriageStageDeps], NodeFn]] = {
    "assess": _assess_node,
    "publish": _publish_node,
}
