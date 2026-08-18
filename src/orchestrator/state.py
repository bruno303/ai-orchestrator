"""Task state and statuses (PLAN.md sections 5 and 6)."""

from __future__ import annotations

from typing import Any, TypedDict

RECEIVED = "RECEIVED"
PREPARING = "PREPARING"
PLANNING = "PLANNING"
IMPLEMENTING = "IMPLEMENTING"
TESTING = "TESTING"
REVIEWING = "REVIEWING"
CREATING_PR = "CREATING_PR"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

ALL_STATUSES = (
    RECEIVED,
    PREPARING,
    PLANNING,
    IMPLEMENTING,
    TESTING,
    REVIEWING,
    CREATING_PR,
    COMPLETED,
    FAILED,
)

VERDICT_APPROVED = "APPROVED"
VERDICT_CHANGES_REQUIRED = "CHANGES_REQUIRED"
VERDICT_NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class InputState(TypedDict, total=False):
    """Provider-owned data received by the workflow."""

    provider: str
    data: dict[str, Any]
    provider_state: dict[str, Any]


class ProcessingState(TypedDict, total=False):
    """Provider-neutral workflow artifacts and phase results."""

    plan_path: str
    plan_summary: str | None
    implementation_result: str | None
    test_result: str | None
    review_result: str | None
    review_verdict: str | None
    provider_state: dict[str, Any]


class WorkspaceState(TypedDict, total=False):
    """Workspace provider state, kept separate from input and output state."""

    provider: str
    path: str
    branch: str
    base_branch: str
    provider_state: dict[str, Any]


class OutputState(TypedDict, total=False):
    """Provider-owned publication result."""

    provider: str
    url: str
    provider_state: dict[str, Any]


class TaskState(TypedDict, total=False):
    task_id: str
    input: InputState
    processing: ProcessingState
    workspace: WorkspaceState
    output: OutputState

    status: str
    question: str | None
    error: str | None
    iteration: int
    phase_attempts: int

    # Deprecated aliases are read-only compatibility for checkpoints created
    # before Task 5. New seeds and graph updates must not write these fields.
    repository: str
    issue_number: int
    issue_title: str
    issue_body: str
    repository_url: str
    base_branch: str
    branch: str
    workspace_path: str
    provider_state: dict[str, Any]
    plan_path: str
    plan_summary: str | None
    implementation_result: str | None
    test_result: str | None
    review_result: str | None
    review_verdict: str | None
    extra_context: list[str]
    pr_number: int | None
