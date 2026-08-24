"""GitHub pull-request publication behind the destination boundary."""

from __future__ import annotations

from typing import Any

from orchestrator.infra.git import client as git
from orchestrator.infra.github import client as github
from orchestrator.domain import ChangeRequest, Context, PublishedChange


def _body(issue_number: int, current_body: str | None = None) -> str:
    """Keep one exact issue-closing reference while preserving the body."""
    closes = f"Closes #{issue_number}"
    if not current_body:
        return closes
    lines = current_body.splitlines()
    remainder_lines: list[str] = []
    skip_blank = False
    for line in lines:
        if line.strip() == closes:
            skip_blank = True
            continue
        if skip_blank and line == "":
            skip_blank = False
            continue
        skip_blank = False
        remainder_lines.append(line)
    remainder = "\n".join(remainder_lines).strip("\n")
    return f"{closes}\n\n{remainder}" if remainder else closes


class GitHubDestination:
    """Commit, push, and create or update the branch's open pull request."""

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        github_client: Any = github,
        git_client: Any = git,
    ) -> None:
        self.options = dict(options or {})
        self.github_client = github_client
        self.git_client = git_client
        self.provider_type = "github"

    @property
    def output_labels(self) -> tuple[str, ...]:
        configured = self.options.get("output_labels")
        if configured is None:
            # Keep direct adapter construction compatible with the historical
            # option while composition now supplies the stage contract.
            configured = (self.options.get("developed_label", "ai-developed"),)
        if isinstance(configured, str):
            return (configured,)
        return tuple(str(label) for label in configured)

    @property
    def remove_output_labels(self) -> tuple[str, ...]:
        configured = self.options.get("remove_output_labels", ())
        if isinstance(configured, str):
            return (configured,)
        return tuple(str(label) for label in configured)

    def _apply_output_labels(self, repository: str, issue_number: int) -> None:
        for label in self.output_labels:
            self.github_client.add_issue_label(repository, issue_number, label)
        for label in self.remove_output_labels:
            self.github_client.remove_issue_label(repository, issue_number, label)

    def publish(self, request: ChangeRequest) -> PublishedChange:
        github_context = request.context.namespace("github")
        git_context = request.context.namespace("git")
        issue_number = github_context.get("issue_number")
        repository = request.repository
        title = request.title
        description = request.description
        source_ref = request.source_ref
        target_ref = request.target_ref
        workspace = git_context.get("workspace")
        context = request.context
        if issue_number is None:
            raise ValueError("GitHub publication requires source issue metadata")
        if not workspace:
            raise ValueError("GitHub publication requires git workspace metadata")
        if not self.git_client.has_changes(workspace) and not self.git_client.commits_ahead(workspace, target_ref):
            raise git.GitError("no changes to commit")
        if self.git_client.has_changes(workspace):
            self.git_client.commit_all(workspace, f"{title}\n\nCloses #{issue_number}")
        self.git_client.push_branch(workspace, source_ref)

        existing_pr = self.github_client.find_open_pr(repository, source_ref)
        if existing_pr is not None:
            current = self.github_client.get_pull_request(repository, existing_pr).body
            body = _body(issue_number, current)
            if body != current:
                self.github_client.update_pull_request_body(repository, existing_pr, body)
            self._apply_output_labels(repository, issue_number)
            return PublishedChange(str(existing_pr), None, self.provider_type, context)

        number = self.github_client.create_pull_request(
            repository, title, _body(issue_number, description),
            head=source_ref, base=target_ref,
        )
        self._apply_output_labels(repository, issue_number)
        return PublishedChange(str(number), provider=self.provider_type, context=context)
