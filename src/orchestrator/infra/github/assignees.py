"""GitHub issue assignee lifecycle helpers."""

from __future__ import annotations

import json
from typing import Any


_PR_TARGET_ERROR = "is a pull request, not an issue"


def clear_authenticated_issue_assignee(
    github_client: Any,
    repository: str,
    number: int,
) -> bool:
    """Remove the configured GitHub identity from an issue, but never from a PR.

    Returns ``True`` when the target is an issue and the removal request was
    sent. Pull requests return ``False`` and keep their assignees unchanged.
    """
    try:
        github_client.get_issue(repository, number)
    except github_client.GitHubError as exc:
        if _PR_TARGET_ERROR in str(exc):
            return False
        raise

    assignee = github_client.get_authenticated_user_login()
    github_client._run_gh(
        [
            "api",
            "--method",
            "DELETE",
            f"repos/{repository}/issues/{number}/assignees",
            "--input",
            "-",
        ],
        input_text=json.dumps({"assignees": [assignee]}),
    )
    return True
