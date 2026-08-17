"""GitHub access via the gh CLI (already authenticated as the user)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


class GitHubError(Exception):
    pass


def _run_gh(args: list[str], *, input_text: str | None = None) -> str:
    proc = subprocess.run(
        ["gh", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _api(endpoint: str, jq_expr: str | None = None, *, paginate: bool = False) -> str:
    args = ["api", endpoint]
    if paginate:
        args.append("--paginate")
    if jq_expr:
        args += ["--jq", jq_expr]
    return _run_gh(args)


def get_repository(repository: str) -> dict:
    """Validate the repository exists; return metadata (ssh_url, default_branch)."""
    out = _api(f"repos/{repository}", "{ssh_url, default_branch, html_url}")
    return json.loads(out)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    html_url: str
    labels: list[str] = field(default_factory=list)


def get_issue(repository: str, number: int) -> Issue:
    out = _api(
        f"repos/{repository}/issues/{number}",
        "{number, title, body, html_url, pull_request}",
    )
    data = json.loads(out)
    if data.get("pull_request"):
        raise GitHubError(f"#{number} in {repository} is a pull request, not an issue")
    return Issue(
        number=data["number"],
        title=data["title"],
        body=data.get("body") or "",
        html_url=data["html_url"],
    )


def list_open_issues(repository: str, label: str | None = None) -> list[Issue]:
    """List open issues (pull requests excluded), optionally filtered by label.

    Uses --paginate without --jq: gh merges each page's raw JSON array, which
    we parse here. (--paginate + --jq is broken for list endpoints: jq is
    applied per page, flattening arrays into concatenated objects.)
    """
    url = f"repos/{repository}/issues?state=open&per_page=100"
    if label:
        url += f"&labels={label}"
    out = _api(url, paginate=True)
    return [
        Issue(
            number=item["number"],
            title=item["title"],
            body=item.get("body") or "",
            html_url=item["html_url"],
            labels=[lbl["name"] for lbl in item.get("labels") or [] if isinstance(lbl, dict)],
        )
        for item in json.loads(out or "[]")
        if item.get("pull_request") is None
    ]


def get_default_branch(repository: str) -> str:
    return get_repository(repository)["default_branch"]


def get_clone_url(repository: str) -> str:
    return get_repository(repository)["ssh_url"]


def create_pull_request(repository: str, title: str, body: str, head: str, base: str) -> int:
    out = _run_gh(
        [
            "pr",
            "create",
            "--repo",
            repository,
            "--title",
            title,
            "--body-file",
            "-",
            "--head",
            head,
            "--base",
            base,
        ],
        input_text=body,
    )
    url = out.strip().splitlines()[-1]
    return int(url.rstrip("/").split("/")[-1])


def update_pull_request_body(repository: str, pr_number: int, body: str) -> None:
    """Replace the body of an open PR (`gh pr edit`)."""
    _run_gh(["pr", "edit", str(pr_number), "--repo", repository, "--body-file", "-"], input_text=body)


def find_open_pr(repository: str, head_branch: str) -> int | None:
    """Return the number of the open PR for `head_branch`, if any."""
    out = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            head_branch,
            "--state",
            "open",
            "--json",
            "number",
        ]
    )
    items = json.loads(out or "[]")
    return int(items[0]["number"]) if items else None


def add_issue_comment(repository: str, number: int, body: str) -> None:
    _run_gh(["api", f"repos/{repository}/issues/{number}/comments", "-f", f"body={body}"])


@dataclass
class IssueComment:
    id: int
    body: str
    user_login: str


def list_issue_comments(repository: str, number: int) -> list[IssueComment]:
    """List comments on an issue or PR conversation (PRs are issues in the API)."""
    out = _api(
        f"repos/{repository}/issues/{number}/comments?per_page=100",
        paginate=True,
    )
    return [
        IssueComment(
            id=item["id"],
            body=item.get("body") or "",
            user_login=(item.get("user") or {}).get("login") or "",
        )
        for item in json.loads(out or "[]")
    ]


@dataclass
class PullRequest:
    number: int
    head_ref: str


@dataclass
class PullRequestDetail:
    number: int
    title: str
    body: str
    url: str
    base_ref: str
    head_ref: str
    files: list[tuple[str, str]]  # (path, status)


def list_open_pull_requests(repository: str) -> list[PullRequest]:
    """List open PRs (used to scan PR conversation comments)."""
    out = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--json",
            "number,headRefName",
        ]
    )
    return [
        PullRequest(number=item["number"], head_ref=item.get("headRefName") or "")
        for item in json.loads(out or "[]")
    ]


def get_pull_request(repository: str, number: int) -> PullRequestDetail:
    """Fetch PR metadata: title, body, URL, base/head refs, changed files."""
    out = _run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repository,
            "--json",
            "number,title,body,url,baseRefName,headRefName,files",
        ]
    )
    data = json.loads(out)
    return PullRequestDetail(
        number=data["number"],
        title=data.get("title") or "",
        body=data.get("body") or "",
        url=data.get("url") or "",
        base_ref=data.get("baseRefName") or "",
        head_ref=data.get("headRefName") or "",
        files=[
            (f.get("path") or "", f.get("status") or "changed")
            for f in data.get("files") or []
            if isinstance(f, dict)
        ],
    )


REACTION_CONTENTS = ("+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes")


def add_reaction(repository: str, comment_id: int, content: str) -> None:
    """Add a reaction to an issue/PR comment (content: eyes, rocket, -1, ...)."""
    if content not in REACTION_CONTENTS:
        raise GitHubError(f"invalid reaction content: {content}")
    _run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repository}/issues/comments/{comment_id}/reactions",
            "-f",
            f"content={content}",
        ]
    )
