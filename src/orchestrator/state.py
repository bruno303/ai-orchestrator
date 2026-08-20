"""Task state and statuses (PLAN.md sections 5 and 6)."""

from __future__ import annotations

from typing import Any, TypedDict

RECEIVED = "RECEIVED"
PREPARING = "PREPARING"
PLANNING = "PLANNING"
IMPLEMENTING = "IMPLEMENTING"
TESTING = "TESTING"
# Deprecated: retained so checkpoints created by older workflow versions can
# still be deserialized. New runs do not enter this status.
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
    REVIEWING,  # Deprecated legacy status; not produced by new runs.
    CREATING_PR,
    COMPLETED,
    FAILED,
)

# Deprecated legacy verdict constants retained for checkpoint compatibility.
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
    # Deprecated legacy fields retained for checkpoint deserialization.
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
    external_id: str
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
    # Deprecated legacy aliases retained for old checkpoints only.
    review_result: str | None
    review_verdict: str | None
    extra_context: list[str]
    pr_number: int | None
