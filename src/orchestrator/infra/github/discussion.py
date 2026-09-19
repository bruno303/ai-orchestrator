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
        self.github_client.add_issue_comment(request.repository, number, request.response)
