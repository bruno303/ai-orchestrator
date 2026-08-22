"""GitHub-owned adapters for the provider-neutral review workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from orchestrator import config, github, workspace
from orchestrator.domain import (
    Context,
    PublishedReview,
    ReviewOutcome,
    ReviewTarget,
)


class GitHubContextPresenter:
    def logging_fields(self, context: Context) -> Mapping[str, Any]:
        values = context.namespace("github")
        return {
            key: values[source]
            for key, source in (
                ("issue", "issue_number"), ("pr", "pr_number"),
                ("author", "author_login"), ("revision", "reviewed_revision"),
            )
            if values.get(source) is not None
        }


@dataclass
class GitHubReviewInputSource:
    github_client: Any = github
    config_module: Any = config
    options: dict[str, Any] = field(default_factory=dict)
    provider_type: str = "github_polling"
    context_presenter: GitHubContextPresenter = field(default_factory=GitHubContextPresenter)

    def poll(self) -> list[ReviewTarget]:
        targets: list[ReviewTarget] = []
        for repository in self.config_module.allowed_repositories():
            repository_state = self._repository_state(repository)
            try:
                prs = self.github_client.list_open_pull_requests(repository)
                for pr in prs:
                    try:
                        detail = self.github_client.get_pull_request(repository, pr.number)
                    except Exception as exc:
                        print(f"[review] {repository}#{pr.number} metadata: {exc}", flush=True)
                        continue
                    if self.options.get("processed_label", "ai-reviewed") in detail.labels:
                        continue
                    target_id = f"review:{repository}#{pr.number}"
                    targets.append(ReviewTarget(
                        id=target_id,
                        repository=repository,
                        title=detail.title,
                        description=detail.body,
                        source_ref=detail.head_ref,
                        target_ref=detail.base_ref,
                        revision=detail.head_sha,
                        input_provider=self.provider_type,
                        context=Context({
                            "github": {
                                "pr_number": pr.number,
                                "url": detail.url,
                                "author_login": getattr(detail, "author_login", ""),
                                "changed_files": [path for path, _ in detail.files],
                                "changed_lines": detail.changed_lines,
                                "reviewed_revision": detail.head_sha,
                            },
                            "git": {
                                "repository_url": repository_state.get("repository_url", ""),
                                "fetch_url": detail.head_clone_url,
                                "workspace": str(workspace.review_workspace(target_id)),
                                "revision": detail.head_sha,
                            },
                        }),
                    ))
            except Exception as exc:
                print(f"[review] {repository} listing: {exc}", flush=True)
        return targets

    def _repository_state(self, repository: str) -> dict[str, str]:
        try:
            metadata = self.github_client.get_repository(repository)
        except Exception:
            return {}
        return {"repository_url": self.github_client.https_clone_url(metadata, repository)}


def _summary(outcome: ReviewOutcome, omitted: tuple[ReviewFinding, ...] = ()) -> str:
    lines = [
        f"## AI Review: {outcome.verdict or 'unknown'}", "",
        outcome.summary or "No summary provided.",
    ]
    if outcome.findings:
        lines += ["", "### Findings"]
        for finding in outcome.findings:
            location = f" ({finding.path}:{finding.line})" if finding.path and finding.line else ""
            lines.append(f"- **{finding.severity}**{location}: {finding.message}")
    if outcome.checks:
        lines += ["", "### Checks"]
        lines.extend(f"- {check.name}: {check.status}" for check in outcome.checks)
    return "\n".join(lines)


class GitHubReviewDestination:
    def __init__(self, options: dict[str, Any] | None = None, github_client: Any = github) -> None:
        self.options = dict(options or {})
        self.github_client = github_client
        self.provider_type = "github"

    def publish(self, target: ReviewTarget, outcome: ReviewOutcome) -> PublishedReview:
        values = target.context.namespace("github")
        number = values.get("pr_number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("GitHub review context is missing PR number")
        changed = set(values.get("changed_files", []))
        diff_lines = values.get("changed_lines", {})
        comments: list[dict[str, Any]] = []
        for finding in outcome.findings:
            path, line = finding.path, finding.line
            side = finding.side or "RIGHT"
            start_side = finding.start_side or side
            valid_lines = diff_lines.get(path, {}).get(side, [])
            start_valid_lines = diff_lines.get(path, {}).get(start_side, [])
            start_line = finding.start_line or line
            if (
                path not in changed or not isinstance(line, int) or isinstance(line, bool)
                or not isinstance(start_line, int) or isinstance(start_line, bool)
                or start_line > line or side not in {"LEFT", "RIGHT"}
                or start_side not in {"LEFT", "RIGHT"}
                or start_line not in start_valid_lines
                or any(value not in valid_lines for value in range(start_line, line + 1))
            ):
                continue
            comment: dict[str, Any] = {
                "path": path, "line": line, "side": side, "body": finding.message,
            }
            if start_line != line:
                comment.update({"start_line": start_line, "start_side": start_side})
            comments.append(comment)
        event = {
            "approve": "APPROVE", "request_changes": "REQUEST_CHANGES",
        }.get(outcome.verdict, "COMMENT")
        authenticated_login = ""
        if event != "COMMENT" and values.get("author_login"):
            authenticated_login = self.github_client.get_authenticated_user_login()
            if authenticated_login == values["author_login"]:
                event = "COMMENT"
        self.github_client.publish_pull_request_review(
            target.repository,
            number,
            _summary(outcome),
            comments,
            event,
            commit_id=target.revision or None,
        )
        self.github_client.add_pull_request_label(
            target.repository, number, self.options.get("processed_label", "ai-reviewed")
        )
        return PublishedReview(str(number), values.get("url"), self.provider_type, target.context)
