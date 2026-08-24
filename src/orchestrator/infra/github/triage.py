"""GitHub adapters for issue triage."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from orchestrator.domain import Context, PublishedTriage, TriageOutcome, TriageTarget
from orchestrator.infra.github import client as github

@dataclass
class GitHubTriageInputSource:
    github_client: Any = github
    config_module: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    provider_type: str = "github_polling"

    @property
    def select_labels(self) -> tuple[str, ...]:
        configured = self.options.get("select_labels", ())
        if isinstance(configured, str):
            return (configured,)
        return tuple(str(label) for label in configured)

    @property
    def suppress_labels(self) -> tuple[str, ...]:
        configured = self.options.get(
            "suppress_labels", ("ai-agent", "ai-triage", "ai-developed")
        )
        if isinstance(configured, str):
            return (configured,)
        return tuple(str(label) for label in configured)

    def poll(self) -> list[TriageTarget]:
        if self.config_module is None:
            raise RuntimeError("GitHubTriageInputSource requires an allowlist configuration")
        targets: list[TriageTarget] = []
        for repository in self.config_module.allowed_repositories():
            try:
                query = {}
                if len(self.select_labels) == 1:
                    query["label"] = self.select_labels[0]
                issues = self.github_client.list_open_issues(repository, **query)
            except self.github_client.GitHubError as exc:
                print(f"[triage] {repository}: {exc}", flush=True)
                continue
            for issue in issues:
                labels = set(issue.labels)
                if not set(self.select_labels).issubset(labels):
                    continue
                if set(self.suppress_labels).intersection(labels):
                    continue
                targets.append(TriageTarget(
                    id=f"triage:{repository}#{issue.number}",
                    repository=repository,
                    title=issue.title,
                    description=issue.body,
                    input_provider=self.provider_type,
                    context=Context({
                        "github": {
                            "issue_number": issue.number,
                            "url": issue.html_url,
                            "labels": list(issue.labels),
                        },
                    }),
                ))
        return targets


def _comment(outcome: TriageOutcome, marker: str) -> str:
    lines = [
        marker,
        "## AI Triage: not ready",
        "",
        f"**Confidence:** {outcome.confidence or 'unknown'}",
        f"**Enough context:** {'yes' if outcome.enough_context else 'no'}",
        "",
        outcome.summary or "No conclusion provided.",
    ]
    if outcome.missing_context:
        lines += ["", "### Missing context", *[f"- {item}" for item in outcome.missing_context]]
    return "\n".join(lines)


class GitHubTriageDestination:
    def __init__(
        self,
        options: dict[str, Any] | None = None,
        github_client: Any = github,
    ) -> None:
        self.options = dict(options or {})
        self.github_client = github_client
        self.provider_type = "github"

    def _output(self, name: str, default: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        configured = self.options.get(name, default)
        if not isinstance(configured, Mapping):
            raise ValueError(f"GitHub triage {name} must be a mapping with add/remove labels")
        add = configured.get("add", default["add"])
        remove = configured.get("remove", default["remove"])
        if isinstance(add, str):
            add = (add,)
        if isinstance(remove, str):
            remove = (remove,)
        return tuple(str(label) for label in add), tuple(str(label) for label in remove)

    def _apply_output(self, repository: str, number: int, name: str, default: Mapping[str, tuple[str, ...]]) -> None:
        add, remove = self._output(name, default)
        for label in add:
            self.github_client.add_issue_label(repository, number, label)
        for label in remove:
            self.github_client.remove_issue_label(repository, number, label)

    def publish(self, target: TriageTarget, outcome: TriageOutcome) -> PublishedTriage:
        number = target.context.namespace("github").get("issue_number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("GitHub triage context is missing issue number")
        if outcome.success and outcome.ready:
            self._apply_output(
                target.repository,
                number,
                "ready_output",
                {"add": ("ai-agent",), "remove": ("ai-triage",)},
            )
        elif outcome.success:
            marker = _marker(target, outcome)
            comments = getattr(self.github_client, "list_issue_comments", None)
            already_published = False
            if callable(comments):
                already_published = any(marker in comment.body for comment in comments(target.repository, number))
            if not already_published:
                self.github_client.add_issue_comment(target.repository, number, _comment(outcome, marker))
            self._apply_output(
                target.repository,
                number,
                "blocked_output",
                {"add": ("ai-triage",), "remove": ()},
            )
        return PublishedTriage(
            str(number), target.context.namespace("github").get("url"), self.provider_type,
            target.context.merged(outcome.context),
        )


def _marker(target: TriageTarget, outcome: TriageOutcome) -> str:
    payload = json.dumps({
        "enough_context": outcome.enough_context,
        "confidence": outcome.confidence,
        "summary": outcome.summary,
        "missing_context": outcome.missing_context,
    }, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- ai-triage:{target.id}:{digest} -->"
