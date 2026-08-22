"""Provider-neutral review contracts."""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.domain.context import Context


@dataclass(frozen=True)
class ReviewTarget:
    id: str
    repository: str
    title: str = ""
    description: str = ""
    source_ref: str = ""
    target_ref: str = ""
    revision: str = ""
    input_provider: str = ""
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("review target id must be a non-empty string")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("review target repository must be a non-empty string")
        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "repository", self.repository.strip())
        if not isinstance(self.context, Context):
            object.__setattr__(self, "context", Context.from_dict(self.context))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "repository": self.repository, "title": self.title,
            "description": self.description, "source_ref": self.source_ref,
            "target_ref": self.target_ref, "revision": self.revision,
            "input_provider": self.input_provider, "context": self.context.to_dict(),
        }

@dataclass(frozen=True)
class ReviewFinding:
    message: str
    severity: str = "info"
    path: str | None = None
    line: int | None = None
    side: str | None = None
    start_line: int | None = None
    start_side: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in {"info", "warning", "error"}:
            raise ValueError(f"invalid review finding severity: {self.severity}")
        for side in (self.side, self.start_side):
            if side is not None and side not in {"LEFT", "RIGHT"}:
                raise ValueError(f"invalid review finding side: {side}")

    def to_dict(self) -> dict[str, object]:
        return {
            "message": self.message, "severity": self.severity, "path": self.path,
            "line": self.line, "side": self.side, "start_line": self.start_line,
            "start_side": self.start_side,
        }


@dataclass(frozen=True)
class ReviewCheck:
    name: str
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "skip"}:
            raise ValueError(f"invalid review check status: {self.status}")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status}


@dataclass(frozen=True)
class ReviewOutcome:
    success: bool
    verdict: str = ""
    summary: str = ""
    findings: tuple[ReviewFinding, ...] = ()
    checks: tuple[ReviewCheck, ...] = ()
    context: Context = field(default_factory=Context)

    def __post_init__(self) -> None:
        if self.verdict and self.verdict not in {"approve", "request_changes", "comment"}:
            raise ValueError(f"invalid review verdict: {self.verdict}")
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "checks", tuple(self.checks))
        if not isinstance(self.context, Context):
            object.__setattr__(self, "context", Context.from_dict(self.context))

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success, "verdict": self.verdict, "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
            "checks": [check.to_dict() for check in self.checks],
            "context": self.context.to_dict(),
        }


@dataclass(frozen=True)
class PublishedReview:
    id: str | None = None
    url: str | None = None
    provider: str = ""
    context: Context = field(default_factory=Context)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "url": self.url, "provider": self.provider,
            "context": self.context.to_dict(),
        }
