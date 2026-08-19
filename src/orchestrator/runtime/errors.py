"""Typed failures raised by runtime operations."""

from __future__ import annotations

from typing import Any


class RuntimeOperationError(Exception):
    """Base class for failures that a workflow can route explicitly."""

    def __init__(
        self,
        message: str,
        *,
        provider_state: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_state = dict(provider_state or {})
        self.attempts = attempts


class WorkspacePreparationError(RuntimeOperationError):
    """The requested workspace could not be prepared."""


class AgentExecutionError(RuntimeOperationError):
    """An agent phase failed or exhausted its retry policy."""


class PlanValidationError(RuntimeOperationError):
    """The planning phase did not produce a usable plan artifact."""


class QualityGateError(AgentExecutionError):
    """The standalone test or quality-gate phase failed."""


class PublicationError(RuntimeOperationError):
    """Issue-workflow publication failed."""


class ReviewExecutionError(RuntimeOperationError):
    """The review agent failed or returned invalid structured output."""


class ReviewPublicationError(RuntimeOperationError):
    """Review publication failed before the target was marked processed."""


class CleanupError(RuntimeOperationError):
    """Workspace cleanup failed."""
