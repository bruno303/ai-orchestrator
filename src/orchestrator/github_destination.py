"""GitHub pull-request publication behind the destination boundary."""

from __future__ import annotations

from typing import Any

from orchestrator import git, github
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

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.provider_type = "github"

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
        if not git.has_changes(workspace) and not git.commits_ahead(workspace, target_ref):
            raise git.GitError("no changes to commit")
        if git.has_changes(workspace):
            git.commit_all(workspace, f"{title}\n\nCloses #{issue_number}")
        git.push_branch(workspace, source_ref)

        existing_pr = github.find_open_pr(repository, source_ref)
        if existing_pr is not None:
            current = github.get_pull_request(repository, existing_pr).body
            body = _body(issue_number, current)
            if body != current:
                github.update_pull_request_body(repository, existing_pr, body)
            return PublishedChange(str(existing_pr), None, self.provider_type, context)

        number = github.create_pull_request(
            repository, title, _body(issue_number, description),
            head=source_ref, base=target_ref,
        )
        return PublishedChange(str(number), provider=self.provider_type, context=context)
