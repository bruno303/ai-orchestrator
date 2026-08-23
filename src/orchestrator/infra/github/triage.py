"""GitHub adapters for issue triage."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from orchestrator.domain import Context, PublishedTriage, TriageOutcome, TriageTarget
from orchestrator.infra.github import client as github


def _repository_ready_label(
    config_module: Any,
    repository: str,
    options: dict[str, Any],
) -> str | None:
    repository_label = getattr(config_module, "repository_label", None)
    if callable(repository_label):
        configured = repository_label(repository)
        if configured:
            return str(configured)
    configured = options.get("ready_label")
    return str(configured) if configured else None


@dataclass
class GitHubTriageInputSource:
    github_client: Any = github
    config_module: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    provider_type: str = "github_polling"

    def poll(self) -> list[TriageTarget]:
        if self.config_module is None:
            raise RuntimeError("GitHubTriageInputSource requires an allowlist configuration")
        targets: list[TriageTarget] = []
        for repository in self.config_module.allowed_repositories():
            excluded = {
                self.options.get("triage_label", "ai-triage"),
                self.options.get("developed_label", "ai-developed"),
            }
            ready_label = _repository_ready_label(self.config_module, repository, self.options)
            excluded.add(ready_label or str(self.options.get("ready_label", "ai-agent")))
            try:
                issues = self.github_client.list_open_issues(repository)
            except self.github_client.GitHubError as exc:
                print(f"[triage] {repository}: {exc}", flush=True)
                continue
            for issue in issues:
                if excluded.intersection(issue.labels):
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
        config_module: Any = None,
    ) -> None:
        self.options = dict(options or {})
        self.github_client = github_client
        self.config_module = config_module
        self.provider_type = "github"

    def publish(self, target: TriageTarget, outcome: TriageOutcome) -> PublishedTriage:
        number = target.context.namespace("github").get("issue_number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("GitHub triage context is missing issue number")
        ready_label = _repository_ready_label(self.config_module, target.repository, self.options)
        triage_label = str(self.options.get("triage_label", "ai-triage"))
        if outcome.success and outcome.ready:
            if ready_label:
                self.github_client.add_issue_label(target.repository, number, ready_label)
            self.github_client.remove_issue_label(target.repository, number, triage_label)
        elif outcome.success:
            marker = _marker(target, outcome)
            comments = getattr(self.github_client, "list_issue_comments", None)
            already_published = False
            if callable(comments):
                already_published = any(marker in comment.body for comment in comments(target.repository, number))
            if not already_published:
                self.github_client.add_issue_comment(target.repository, number, _comment(outcome, marker))
            self.github_client.add_issue_label(target.repository, number, triage_label)
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
