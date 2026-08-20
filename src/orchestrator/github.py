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
    labels: list[str] = field(default_factory=list)
    head_sha: str = ""
    head_clone_url: str = ""
    changed_lines: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    author_login: str = ""


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
            "number,title,body,url,baseRefName,headRefName,headRefOid,headRepository,author,files,labels",
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
        labels=[label.get("name", "") for label in data.get("labels") or [] if isinstance(label, dict)],
        head_sha=data.get("headRefOid") or "",
        head_clone_url=((data.get("headRepository") or {}).get("sshUrl") or ""),
        changed_lines=_changed_lines(data.get("files") or []),
        author_login=(data.get("author") or {}).get("login") or "",
    )


def get_authenticated_user_login() -> str:
    """Return the login for the GitHub account used by the gh CLI."""
    return _api("user", "login").strip()


def _changed_lines(files: list[dict]) -> dict[str, dict[str, list[int]]]:
    """Extract line ranges GitHub accepts for inline review comments."""
    result: dict[str, dict[str, list[int]]] = {}
    for file in files:
        path = file.get("path") or ""
        right: list[int] = []
        left: list[int] = []
        old = new = 0
        for line in (file.get("patch") or "").splitlines():
            if line.startswith("@@"):
                import re
                match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", line)
                if match:
                    old, new = int(match.group(1)), int(match.group(2))
                continue
            if line.startswith("+") and not line.startswith("+++"):
                right.append(new); new += 1
            elif line.startswith("-") and not line.startswith("---"):
                left.append(old); old += 1
            elif line.startswith(" "):
                old += 1; new += 1
        if path:
            result[path] = {"LEFT": left, "RIGHT": right}
    return result


def add_pull_request_label(repository: str, number: int, label: str) -> None:
    """Add a label to a pull request."""
    _run_gh(["api", f"repos/{repository}/issues/{number}/labels", "-f", f"labels[]={label}"])


def remove_pull_request_label(repository: str, number: int, label: str) -> None:
    """Remove a label from a pull request when it exists."""
    try:
        _run_gh(["api", "--method", "DELETE", f"repos/{repository}/issues/{number}/labels/{label}"])
    except GitHubError as exc:
        # GitHub returns 404 when the label is already absent; removal is
        # intentionally idempotent for polling/retry workflows.
        message = str(exc).lower()
        if "404" not in message and "not found" not in message:
            raise


@dataclass
class ReviewComment:
    id: int
    body: str
    user_login: str
    path: str = ""
    line: int | None = None


def list_pull_request_review_comments(repository: str, number: int) -> list[ReviewComment]:
    """List inline review comments on a pull request."""
    out = _api(f"repos/{repository}/pulls/{number}/comments?per_page=100", paginate=True)
    return [
        ReviewComment(
            id=item["id"], body=item.get("body") or "",
            user_login=(item.get("user") or {}).get("login") or "",
            path=item.get("path") or "", line=item.get("line"),
        )
        for item in json.loads(out or "[]")
    ]


def publish_pull_request_review(
    repository: str,
    number: int,
    body: str,
    comments: list[dict] | None = None,
    event: str = "COMMENT",
    commit_id: str | None = None,
) -> None:
    """Publish a review summary and optional inline comments in one API call."""
    payload = {"body": body, "event": event, "comments": comments or []}
    if commit_id:
        payload["commit_id"] = commit_id
    _run_gh(
        ["api", "--method", "POST", f"repos/{repository}/pulls/{number}/reviews", "--input", "-"],
        input_text=json.dumps(payload),
    )


# Short aliases keep the adapter vocabulary convenient for callers.
add_label = add_pull_request_label
remove_label = remove_pull_request_label
list_review_comments = list_pull_request_review_comments
create_pull_request_review = publish_pull_request_review


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
