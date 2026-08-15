"""Task state and statuses (PLAN.md sections 5 and 6)."""

from __future__ import annotations

from typing import TypedDict

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


class TaskState(TypedDict, total=False):
    task_id: str
    repository: str
    issue_number: int
    issue_title: str
    issue_body: str

    repository_url: str
    base_branch: str
    branch: str
    workspace: str

    plan_path: str
    plan_summary: str | None
    implementation_result: str | None
    test_result: str | None
    review_result: str | None
    review_verdict: str | None

    extra_context: list[str]

    status: str
    question: str | None

    pr_number: int | None
    error: str | None

    iteration: int
    phase_attempts: int