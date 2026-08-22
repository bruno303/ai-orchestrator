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

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "metadata": self.metadata}


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "repository": self.repository, "title": self.title,
            "description": self.description, "source_ref": self.source_ref,
            "target_ref": self.target_ref, "context": self.context.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class PublishedChange:
    id: str | None = None
    url: str | None = None
    provider: str = ""
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "url": self.url, "provider": self.provider,
            "context": self.context.to_dict(),
        }
