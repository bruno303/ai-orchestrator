"""Typed failures raised by runtime operations."""

from __future__ import annotations

from orchestrator.domain import Context


class RuntimeOperationError(Exception):
    """Base class for failures that a workflow can route explicitly."""

    def __init__(
        self,
        message: str,
        *,
        context: Context | None = None,
    ) -> None:
        super().__init__(message)
        self.context = context or Context()


class WorkspacePreparationError(RuntimeOperationError):
    """The requested workspace could not be prepared."""


class AgentExecutionError(RuntimeOperationError):
    """An agent phase failed or exhausted its retry policy."""


class PlanValidationError(RuntimeOperationError):
    """The planning phase did not produce a usable plan artifact."""


class PublicationError(RuntimeOperationError):
    """Issue-workflow publication failed."""


class ReviewExecutionError(RuntimeOperationError):
    """The review agent failed or returned invalid structured output."""


class ReviewPublicationError(RuntimeOperationError):
    """Review publication failed before the target was marked processed."""


class CleanupError(RuntimeOperationError):
    """Workspace cleanup failed."""
