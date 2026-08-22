"""GitHub-backed input source for issue and comment polling."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator import config, github, state as state_mod, workspace
from orchestrator.domain import Context, WorkItem
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


class GitHubSourceFeedback:
    """Translate semantic input lifecycle events to GitHub comment behavior."""

    def __init__(self, store: Any, github_client: Any = github) -> None:
        self.store = store
        self.github_client = github_client

    def _reaction(self, event: InputEvent, content: str) -> None:
        comment_id = event.context.namespace("github").get("comment_id")
        if comment_id is None:
            return
        try:
            self.github_client.add_reaction(event.work_item.repository, comment_id, content)
        except self.github_client.GitHubError:
            pass

    def mark_started(self, event: InputEvent) -> None:
        github_context = event.context.namespace("github")
        comment_id = github_context.get("comment_id")
        if comment_id is not None:
            self.store.mark_comment_handled(
                comment_id,
                event.work_item.id,
                event.work_item.repository,
                event.work_item.context.namespace("github")["issue_number"],
                "STARTED",
            )
        self._reaction(event, "eyes")

    def mark_succeeded(self, event: InputEvent) -> None:
        comment_id = event.context.namespace("github").get("comment_id")
        if comment_id is not None:
            self.store.update_comment_status(comment_id, state_mod.COMPLETED)
        self._reaction(event, "rocket")

    def mark_failed(self, event: InputEvent, error: str | None = None) -> None:
        comment_id = event.context.namespace("github").get("comment_id")
        if comment_id is not None:
            self.store.update_comment_status(comment_id, state_mod.FAILED)
        self._reaction(event, "-1")


@dataclass
class GitHubPollingInputSource:
    """Translate the existing GitHub polling protocol into input events."""

    provider_type = "github_polling"
    store: Any
    github_client: Any = github
    config_module: Any = config
    options: dict[str, Any] = field(default_factory=dict)
    feedback: Any = None

    def poll(self) -> list[InputEvent]:
        events: list[InputEvent] = []
        for repository in self.config_module.allowed_repositories():
            repository_state = self._repository_state(repository)
            try:
                issues = self.github_client.list_open_issues(repository)
            except self.github_client.GitHubError as exc:
                print(f"[poll] {repository}: {exc}", flush=True)
                continue

            for issue in issues:
                events.extend(self._comment_events(
                    repository, issue.number, f"{repository}#{issue.number}",
                    git_context=repository_state,
                ))

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
                            repository, pr.number, f"{repository}#{issue_number}",
                            task_number=issue_number, pr_number=pr.number,
                            git_context=repository_state,
                        )
                    )

            label = self.config_module.repository_label(repository)
            if label:
                issues = [issue for issue in issues if label in issue.labels]
            for issue in issues:
                task_id = f"{repository}#{issue.number}"
                if self.store.exists_task(task_id):
                    continue
                context = Context({
                    "github": {"issue_number": issue.number},
                    "git": {
                        **repository_state,
                        "branch": f"ai/issue-{issue.number}",
                        "workspace": str(workspace.task_workspace(task_id)),
                    },
                })
                events.append(
                    InputEvent(
                        event_id=f"issue:{repository}#{issue.number}",
                        work_item=WorkItem(
                            task_id, repository, issue.title, issue.body,
                            input_provider=self.provider_type, context=context,
                        ),
                        metadata={"kind": "issue"},
                        trigger="new",
                    )
                )
        return events

    def _comment_events(
        self,
        repository: str,
        number: int,
        task_id: str,
        pr_number: int | None = None,
        task_number: int | None = None,
        git_context: dict[str, Any] | None = None,
    ) -> list[InputEvent]:
        try:
            comments = self.github_client.list_issue_comments(repository, number)
        except self.github_client.GitHubError as exc:
            print(f"[poll] {repository}#{number}: comments: {exc}", flush=True)
            return []
        command = self.config_module.repository_command(repository)
        task_number = task_number or int(task_id.rsplit("#", 1)[1])
        eligible = [
            comment
            for comment in comments
            if comment.body.strip().startswith(command)
            and not self.store.is_comment_handled(
                comment.id, stale_after_seconds=self.config_module.STALE_SECONDS
            )
            and not (
                (task := self.store.get_task(task_id))
                and task["status"] in ACTIVE_STATUSES
            )
        ]
        if not eligible:
            return []
        try:
            issue = self.github_client.get_issue(repository, task_number)
        except self.github_client.GitHubError as exc:
            print(f"[poll] {repository}#{task_number}: issue: {exc}", flush=True)
            return []
        if pr_number is None:
            try:
                pr_number = self.github_client.find_open_pr(repository, f"ai/issue-{task_number}")
            except self.github_client.GitHubError:
                pr_number = None
        context: list[str] = []
        if pr_number is not None:
            try:
                context.append(_pr_context_block(self.github_client.get_pull_request(repository, pr_number)))
            except self.github_client.GitHubError:
                pass
        events: list[InputEvent] = []
        for comment in eligible:
            events.append(
                InputEvent(
                    event_id=f"comment:{comment.id}",
                    work_item=WorkItem(
                        task_id, repository, issue.title, comment.body,
                        tuple([*context, comment.body]), self.provider_type,
                        Context({
                            "github": {"issue_number": task_number},
                            "git": {
                                **(git_context or {}),
                                "branch": f"ai/issue-{task_number}",
                                "workspace": str(workspace.task_workspace(task_id)),
                            },
                        }),
                    ),
                    metadata={
                        "kind": "comment",
                    },
                    trigger="rerun",
                    context=Context({"github": {
                        "comment_id": comment.id,
                        **({"pr_number": pr_number} if pr_number is not None else {}),
                    }}),
                )
            )
        return events

    def _repository_state(self, repository: str) -> dict[str, Any]:
        try:
            metadata = self.github_client.get_repository(repository)
        except (AttributeError, self.github_client.GitHubError):
            return {}
        return {
            "repository_url": self.github_client.https_clone_url(metadata, repository),
            "base_branch": metadata.get("default_branch", ""),
        }
