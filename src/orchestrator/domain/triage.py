"""Provider-neutral issue triage contracts."""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.domain.context import Context


CONFIDENCE_LEVELS = {"low", "medium", "high"}


@dataclass(frozen=True)
class TriageTarget:
    id: str
    repository: str
    title: str = ""
    description: str = ""
    input_provider: str = ""
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("triage target id must be a non-empty string")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("triage target repository must be a non-empty string")
        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "repository", self.repository.strip())
        if not isinstance(self.context, Context):
            object.__setattr__(self, "context", Context.from_dict(self.context))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "repository": self.repository,
            "title": self.title,
            "description": self.description,
            "input_provider": self.input_provider,
            "context": self.context.to_dict(),
        }


@dataclass(frozen=True)
class TriageOutcome:
    success: bool
    enough_context: bool = False
    confidence: str = ""
    summary: str = ""
    missing_context: tuple[str, ...] = ()
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        if self.confidence and self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"invalid triage confidence: {self.confidence}")
        if not isinstance(self.enough_context, bool):
            raise TypeError("triage enough_context must be a boolean")
        if not isinstance(self.summary, str):
            raise TypeError("triage summary must be a string")
        object.__setattr__(self, "missing_context", tuple(self.missing_context))
        if any(not isinstance(item, str) for item in self.missing_context):
            raise TypeError("triage missing_context must contain only strings")
        if not isinstance(self.context, Context):
            object.__setattr__(self, "context", Context.from_dict(self.context))

    @property
    def ready(self) -> bool:
        return self.success and self.enough_context and self.confidence == "high"

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "enough_context": self.enough_context,
            "confidence": self.confidence,
            "summary": self.summary,
            "missing_context": list(self.missing_context),
            "context": self.context.to_dict(),
        }


@dataclass(frozen=True)
class PublishedTriage:
    id: str | None = None
    url: str | None = None
    provider: str = ""
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "url": self.url,
            "provider": self.provider,
            "context": self.context.to_dict(),
        }
