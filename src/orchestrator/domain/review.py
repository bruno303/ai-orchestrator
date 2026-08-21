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

    @property
    def event_id(self) -> str:
        """Deprecated event-wrapper alias."""
        return self.id

    @property
    def task_id(self) -> str:
        """Deprecated runtime request alias."""
        return self.id

    @property
    def body(self) -> str:
        return self.description

    @property
    def provider_state(self) -> dict[str, object]:
        """Deprecated flat view for callers predating namespaced Context."""
        github = dict(self.context.namespace("github"))
        git = dict(self.context.namespace("git"))
        if "pr_number" in github:
            github["number"] = github["pr_number"]
        if self.source_ref:
            git.setdefault("head_ref", self.source_ref)
        if self.target_ref:
            git.setdefault("base_ref", self.target_ref)
        if self.revision:
            git.setdefault("head_sha", self.revision)
        return {**git, **github}


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


@dataclass(frozen=True)
class ReviewCheck:
    name: str
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "skip"}:
            raise ValueError(f"invalid review check status: {self.status}")


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


@dataclass(frozen=True)
class PublishedReview:
    id: str | None = None
    url: str | None = None
    provider: str = ""
    context: Context = field(default_factory=Context)
