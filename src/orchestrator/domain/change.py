"""Provider-neutral change publication contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.domain.context import Context


@dataclass(frozen=True)
class Artifact:
    path: str
    kind: str = "file"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangeRequest:
    task_id: str
    repository: str
    title: str
    description: str
    source_ref: str
    target_ref: str
    context: Context
    artifacts: tuple[Artifact, ...] = ()


@dataclass(frozen=True)
class PublishedChange:
    id: str | None = None
    url: str | None = None
    provider: str = ""
    context: Context = field(default_factory=Context)

    @property
    def number(self) -> int | None:
        """Deprecated GitHub compatibility view; canonical identity is ``id``."""
        return int(self.id) if self.id and self.id.isdigit() else None
