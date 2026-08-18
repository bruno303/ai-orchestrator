"""GitHub pull-request publication behind the destination boundary."""

from __future__ import annotations

from typing import Any

from orchestrator import git, github
from orchestrator.providers import PublicationRequest, PublicationResult


def _body(issue_number: int, provider_state: dict, current_body: str | None = None) -> str:
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

    def publish(self, request: PublicationRequest) -> PublicationResult:
        provider_state = {**self.options, **request.provider_state}
        workspace = provider_state["workspace"]
        if not git.has_changes(workspace) and not git.commits_ahead(workspace, request.base):
            raise git.GitError("no changes to commit")
        if git.has_changes(workspace):
            git.commit_all(workspace, f"{request.title}\n\nCloses #{provider_state['issue_number']}")
        git.push_branch(workspace, request.head)

        existing_pr = github.find_open_pr(request.repository, request.head)
        if existing_pr is not None:
            current = github.get_pull_request(request.repository, existing_pr).body
            body = _body(provider_state["issue_number"], provider_state, current)
            if body != current:
                github.update_pull_request_body(request.repository, existing_pr, body)
            return PublicationResult(number=existing_pr, url=None)

        number = github.create_pull_request(
            request.repository, request.title, request.body,
            head=request.head, base=request.base,
        )
        return PublicationResult(number=number)
