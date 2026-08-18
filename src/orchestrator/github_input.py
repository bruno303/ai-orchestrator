"""GitHub-backed input source for issue and comment polling."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator import config, github, state as state_mod
from orchestrator.providers import InputEvent


ACTIVE_STATUSES = {
    state_mod.RECEIVED,
    state_mod.PREPARING,
    state_mod.PLANNING,
    state_mod.IMPLEMENTING,
    state_mod.TESTING,
    state_mod.CREATING_PR,
}


def _pr_context_block(pr: github.PullRequestDetail) -> str:
    files = ", ".join(f"{path} ({status})" for path, status in pr.files[:20]) or "n/a"
    return (
        "<pr>\n"
        f"number: {pr.number}\n"
        f"title: {pr.title}\n"
        f"url: {pr.url}\n"
        f"base: {pr.base_ref} -> head: {pr.head_ref}\n"
        f"description: {pr.body}\n"
        f"changed files: {files}\n"
        "</pr>"
    )


@dataclass
class GitHubPollingInputSource:
    """Translate the existing GitHub polling protocol into input events."""

    provider_type = "github_polling"
    store: Any
    github_client: Any = github
    config_module: Any = config
    options: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> list[InputEvent]:
        events: list[InputEvent] = []
        for repository in self.config_module.allowed_repositories():
            try:
                issues = self.github_client.list_open_issues(repository)
            except self.github_client.GitHubError as exc:
                print(f"[poll] {repository}: {exc}", flush=True)
                continue

            for issue in issues:
                events.extend(self._comment_events(repository, issue.number, f"{repository}#{issue.number}"))

            try:
                prs = self.github_client.list_open_pull_requests(repository)
            except self.github_client.GitHubError as exc:
                print(f"[poll] {repository}: prs: {exc}", flush=True)
                prs = []
            for pr in prs:
                match = re.match(r"^ai/issue-(\d+)$", pr.head_ref)
                if match:
                    issue_number = int(match.group(1))
                    events.extend(
                        self._comment_events(
                            repository, pr.number, f"{repository}#{issue_number}", pr_number=pr.number
                        )
                    )

            label = self.config_module.repository_label(repository)
            if label:
                issues = [issue for issue in issues if label in issue.labels]
            for issue in issues:
                if self.store.exists(repository, issue.number):
                    continue
                events.append(
                    InputEvent(
                        event_id=f"issue:{repository}#{issue.number}",
                        repository=repository,
                        number=issue.number,
                        title=issue.title,
                        body=issue.body,
                        metadata={"kind": "issue"},
                    )
                )
        return events

    def _comment_events(
        self, repository: str, number: int, task_id: str, pr_number: int | None = None
    ) -> list[InputEvent]:
        try:
            comments = self.github_client.list_issue_comments(repository, number)
        except self.github_client.GitHubError as exc:
            print(f"[poll] {repository}#{number}: comments: {exc}", flush=True)
            return []
        command = self.config_module.repository_command(repository)
        events: list[InputEvent] = []
        for comment in comments:
            if not comment.body.strip().startswith(command):
                continue
            if self.store.is_comment_handled(comment.id, stale_after_seconds=self.config_module.STALE_SECONDS):
                continue
            task = self.store.get_task(task_id)
            if task and task["status"] in ACTIVE_STATUSES:
                continue
            events.append(
                InputEvent(
                    event_id=f"comment:{comment.id}",
                    repository=repository,
                    number=int(task_id.rsplit("#", 1)[1]),
                    title="",
                    body=comment.body,
                    metadata={"kind": "comment", "comment": comment, "pr_number": pr_number},
                )
            )
        return events
