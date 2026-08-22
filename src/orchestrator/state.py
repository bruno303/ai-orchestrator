"""Task state and statuses (PLAN.md sections 5 and 6)."""

from __future__ import annotations

from typing import Any, TypedDict

RECEIVED = "RECEIVED"
PREPARING = "PREPARING"
PLANNING = "PLANNING"
IMPLEMENTING = "IMPLEMENTING"
TESTING = "TESTING"
CREATING_PR = "CREATING_PR"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

ALL_STATUSES = (
    RECEIVED,
    PREPARING,
    PLANNING,
    IMPLEMENTING,
    TESTING,
    CREATING_PR,
    COMPLETED,
    FAILED,
)

class InputState(TypedDict, total=False):
    """Provider-owned data received by the workflow."""

    provider: str
    data: dict[str, Any]
    context: dict[str, dict[str, Any]]


class ProcessingState(TypedDict, total=False):
    """Provider-neutral workflow artifacts and phase results."""

    plan_path: str
    plan_summary: str | None
    implementation_result: str | None
    test_result: str | None
    context: dict[str, dict[str, Any]]


class WorkspaceState(TypedDict, total=False):
    """Workspace provider state, kept separate from input and output state."""

    provider: str
    path: str
    branch: str
    base_branch: str
    context: dict[str, dict[str, Any]]


class OutputState(TypedDict, total=False):
    """Provider-owned publication result."""

    provider: str
    external_id: str
    url: str
    context: dict[str, dict[str, Any]]


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
