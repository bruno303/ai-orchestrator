"""Execution work-item identity and data."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from orchestrator.domain.context import Context


def ensure_task_id(value: str | None) -> str:
    """Return a stable supplied ID, or create one once at an input boundary."""
    if value is not None and value.strip():
        return value.strip()
    return str(uuid4())


@dataclass(frozen=True)
class WorkItem:
    id: str
    repository: str
    title: str
    description: str = ""
    extra_context: tuple[str, ...] = ()
    input_provider: str = ""
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("work item id must be a non-empty string")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("work item repository must be a non-empty string")
        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "repository", self.repository.strip())
        object.__setattr__(self, "extra_context", tuple(self.extra_context))
        if not isinstance(self.context, Context):
            object.__setattr__(self, "context", Context.from_dict(self.context))
