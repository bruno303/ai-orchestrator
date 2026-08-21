"""GitHub adapters for the provider-neutral review workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator import config, github, workspace
from orchestrator.providers import ReviewEvent, ReviewRequest, ReviewResult


@dataclass
class GitHubReviewInputSource:
    github_client: Any = github
    config_module: Any = config
    options: dict[str, Any] = field(default_factory=dict)
    provider_type: str = "github_polling"

    def poll(self) -> list[ReviewEvent]:
        events: list[ReviewEvent] = []
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
                    events.append(ReviewEvent(
                        event_id=f"review:{repository}#{pr.number}",
                        repository=repository,
                        title=detail.title, body=detail.body,
                        metadata={"url": detail.url},
                        provider_state={
                            "number": pr.number,
                            "workspace": str(workspace.review_workspace(repository, pr.number)),
                            "repository_url": repository_state.get("repository_url", ""),
                            "fetch_url": detail.head_clone_url,
                            "revision": detail.head_sha,
                            "head_ref": detail.head_ref, "base_ref": detail.base_ref,
                        "head_sha": detail.head_sha,
                            "head_clone_url": detail.head_clone_url,
                            "author_login": getattr(detail, "author_login", ""),
                            "changed_files": [path for path, _ in detail.files],
                            "changed_lines": detail.changed_lines,
                        },
                    ))
            except Exception as exc:
                print(f"[review] {repository} listing: {exc}", flush=True)
        return events

    def _repository_state(self, repository: str) -> dict[str, str]:
        try:
            metadata = self.github_client.get_repository(repository)
        except Exception:
            return {}
        return {"repository_url": self.github_client.https_clone_url(metadata, repository)}


def _summary(result: ReviewResult) -> str:
    findings = result.comments
    checks = result.checks
    lines = [f"## AI Review: {result.verdict or 'unknown'}", "", result.summary or "No summary provided."]
    if findings:
        lines += ["", "### Findings"]
        for finding in findings:
            location = ""
            if finding.get("path") and finding.get("line"):
                location = f" ({finding['path']}:{finding['line']})"
            lines.append(f"- **{finding.get('severity', 'info')}**{location}: {finding.get('message', finding.get('body', ''))}")
    if checks:
        lines += ["", "### Checks"]
        lines.extend(f"- {check}" if isinstance(check, str) else f"- {check.get('name', 'check')}: {check.get('status', '')}" for check in checks)
    return "\n".join(lines)


class GitHubReviewDestination:
    def __init__(self, options: dict[str, Any] | None = None, github_client: Any = github) -> None:
        self.options = dict(options or {})
        self.github_client = github_client
        self.provider_type = "github"

    def publish(self, request: ReviewRequest, result: ReviewResult) -> None:
        number = request.provider_state.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("review provider state is missing PR number")
        changed = set(request.provider_state.get("changed_files", []))
        comments: list[dict[str, Any]] = []
        diff_lines = request.provider_state.get("changed_lines", {})
        for finding in result.comments:
            path, line = finding.get("path"), finding.get("line")
            side = finding.get("side", "RIGHT")
            start_side = finding.get("start_side", side)
            valid_lines = diff_lines.get(path, {}).get(side, [])
            start_valid_lines = diff_lines.get(path, {}).get(start_side, [])
            start_line = finding.get("start_line", line)
            if (path not in changed or not isinstance(line, int) or isinstance(line, bool) or
                    not isinstance(start_line, int) or isinstance(start_line, bool) or start_line > line or
                    side not in {"LEFT", "RIGHT"} or
                    start_side not in {"LEFT", "RIGHT"} or
                    start_line not in start_valid_lines or
                    any(value not in valid_lines for value in range(start_line, line + 1))):
                continue
            comment = {"path": path, "line": line, "side": side,
                       "body": finding.get("message", finding.get("body", ""))}
            if start_line != line:
                comment["start_line"] = start_line
                comment["start_side"] = start_side
            comments.append(comment)
        verdict = result.verdict.lower()
        event = "APPROVE" if verdict in {"approve", "approved"} else "REQUEST_CHANGES" if verdict in {"request_changes", "changes_requested"} else "COMMENT"
        authenticated_login = ""
        if event != "COMMENT" and request.provider_state.get("author_login"):
            authenticated_login = self.github_client.get_authenticated_user_login()
            if authenticated_login == request.provider_state["author_login"]:
                event = "COMMENT"
        print(
            f"[review] publishing: repository={request.repository} pr={number} "
            f"verdict={verdict} event={event} author={request.provider_state.get('author_login') or '<missing>'} "
            f"authenticated={authenticated_login or '<not checked>'} "
            f"head_sha={request.provider_state.get('head_sha') or '<missing>'} comments={len(comments)}",
            flush=True,
        )
        self.github_client.publish_pull_request_review(
            request.repository,
            number,
            _summary(result),
            comments,
            event,
            commit_id=request.provider_state.get("head_sha"),
        )
        self.github_client.add_pull_request_label(request.repository, number, self.options.get("processed_label", "ai-reviewed"))
