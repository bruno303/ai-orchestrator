"""GitHub adapter for publishing read-only discussion responses."""

from __future__ import annotations

from typing import Any

from orchestrator.application.ports import DiscussionPublicationRequest
from orchestrator.infra.github import client as github


class GitHubDiscussionDestination:
    """Post only the agent's textual answer to the originating conversation."""

    provider_type = "github"

    def __init__(self, options: dict[str, Any] | None = None, github_client: Any = github) -> None:
        self.options = dict(options or {})
        self.github_client = github_client

    def publish(self, request: DiscussionPublicationRequest) -> None:
        values = request.context.namespace("github")
        number = values.get("conversation_number")
        if number is None:
            number = values.get("pr_number") or values.get("issue_number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("GitHub discussion context is missing conversation number")

        comment_id = values.get("comment_id")
        if isinstance(comment_id, int) and not isinstance(comment_id, bool):
            marker = _publication_marker(comment_id)
            comments = getattr(self.github_client, "list_issue_comments", None)
            if callable(comments) and any(
                marker in comment.body for comment in comments(request.repository, number)
            ):
                return
            body = f"{marker}\n{request.response}"
        else:
            # Keep direct callers without an originating comment compatible;
            # polling-triggered discussions always include comment_id.
            body = request.response
        self.github_client.add_issue_comment(request.repository, number, body)


def _publication_marker(comment_id: int) -> str:
    """Identify a response already published for one triggering comment."""
    return f"<!-- ai-agent-discussion:{comment_id} -->"
