"""Tests for the gh CLI wrapper (gh subprocess mocked)."""

from __future__ import annotations

import json

import pytest

from orchestrator import github


@pytest.fixture
def fake_gh(monkeypatch):
    calls: list[list[str]] = []

    def run(args: list[str], input_text: str | None = None) -> str:
        calls.append(args)
        joined = " ".join(args)
        if "issues?state=open" in joined:
            return json.dumps(
                [
                    {"number": 1, "title": "one", "body": "b1", "html_url": "u1"},
                    {"number": 2, "title": "two", "body": "b2", "html_url": "u2", "pull_request": {"url": "x"}},
                ]
            )
        if "issues/7/comments?per_page=100" in joined:
            return json.dumps(
                [
                    {"id": 101, "body": "hello", "user": {"login": "bruno"}},
                    {"id": 102, "body": "/ai-agent do it", "user": {"login": "other"}},
                ]
            )
        if joined.startswith("api repos/company/backend/issues/") and "comments" not in joined:
            return json.dumps({"number": 7, "title": "t", "body": "b", "html_url": "u", "pull_request": None})
        if joined.startswith("api repos/company/backend"):
            return json.dumps({"ssh_url": "git@github.com:company/backend.git", "default_branch": "main", "html_url": "h"})
        if joined.startswith("pr list") and "--head" in joined:
            return json.dumps([{"number": 42}])
        if joined.startswith("pr list"):
            return json.dumps([{"number": 11, "headRefName": "ai/issue-3"}, {"number": 12, "headRefName": "feature/x"}])
        if joined.startswith("pr view"):
            return json.dumps(
                {
                    "number": 14,
                    "title": "feat: backlog",
                    "body": "Closes #28",
                    "url": "https://github.com/company/backend/pull/14",
                    "baseRefName": "main",
                    "headRefName": "ai/issue-28",
                    "files": [
                        {"path": "src/app/page.tsx", "status": "modified"},
                        {"path": "src/new.ts", "status": "added"},
                    ],
                }
            )
        if "POST" in joined and "reactions" in joined:
            return ""
        if joined.startswith("pr create"):
            return "https://github.com/company/backend/pull/42\n"
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(github, "_run_gh", run)
    return calls


def test_get_issue(fake_gh):
    issue = github.get_issue("company/backend", 7)
    assert issue.number == 7
    assert issue.title == "t"
    assert issue.html_url == "u"


def test_get_issue_rejects_pr(monkeypatch):
    def run(args):
        return json.dumps({"number": 7, "title": "t", "body": "b", "html_url": "u", "pull_request": {"url": "x"}})

    monkeypatch.setattr(github, "_run_gh", run)
    with pytest.raises(github.GitHubError, match="pull request"):
        github.get_issue("company/backend", 7)


def test_list_open_issues_excludes_prs(fake_gh):
    issues = github.list_open_issues("company/backend")
    assert [i.number for i in issues] == [1]
    assert fake_gh[0] == ["api", "repos/company/backend/issues?state=open&per_page=100", "--paginate"]


def test_list_open_issues_label_filter(fake_gh):
    issues = github.list_open_issues("company/backend", label="ai-agent")
    assert [i.number for i in issues] == [1]
    assert "labels=ai-agent" in " ".join(fake_gh[0])


def test_list_issue_comments(fake_gh):
    comments = github.list_issue_comments("company/backend", 7)
    assert [c.id for c in comments] == [101, 102]
    assert comments[0].body == "hello"
    assert comments[0].user_login == "bruno"
    assert fake_gh[-1][:2] == ["api", "repos/company/backend/issues/7/comments?per_page=100"]


def test_add_reaction(fake_gh):
    github.add_reaction("company/backend", 101, "eyes")
    args = fake_gh[-1]
    assert args[0] == "api"
    assert args[1] == "--method"
    assert args[2] == "POST"
    assert args[3] == "repos/company/backend/issues/comments/101/reactions"
    assert args[-2:] == ["-f", "content=eyes"]


def test_add_reaction_invalid_content():
    with pytest.raises(github.GitHubError, match="invalid reaction"):
        github.add_reaction("company/backend", 101, "banana")


def test_list_open_pull_requests(fake_gh):
    prs = github.list_open_pull_requests("company/backend")
    assert [(p.number, p.head_ref) for p in prs] == [(11, "ai/issue-3"), (12, "feature/x")]


def test_find_open_pr(fake_gh):
    assert github.find_open_pr("company/backend", "ai/issue-3") == 42


def test_find_open_pr_none(monkeypatch):
    monkeypatch.setattr(github, "_run_gh", lambda args: "[]")
    assert github.find_open_pr("company/backend", "ai/issue-3") is None


def test_get_pull_request(fake_gh):
    pr = github.get_pull_request("company/backend", 14)
    assert pr.number == 14
    assert pr.title == "feat: backlog"
    assert pr.base_ref == "main"
    assert pr.head_ref == "ai/issue-28"
    assert pr.url == "https://github.com/company/backend/pull/14"
    assert pr.files == [("src/app/page.tsx", "modified"), ("src/new.ts", "added")]


def test_list_open_issues_empty(monkeypatch):
    monkeypatch.setattr(github, "_run_gh", lambda args: "[]")
    assert github.list_open_issues("company/backend") == []


def test_get_repository(fake_gh):
    assert github.get_default_branch("company/backend") == "main"
    assert github.get_clone_url("company/backend") == "git@github.com:company/backend.git"


def test_create_pull_request(fake_gh):
    number = github.create_pull_request("company/backend", "feat: x", "Closes #7\n\n## Summary", "ai/issue-7", "main")
    assert number == 42
    assert fake_gh[-1][:3] == ["pr", "create", "--repo"]
    assert "--body-file" in fake_gh[-1]
    assert fake_gh[-1][-1] == "main"


def test_gh_failure_raises(monkeypatch):
    def run(args):
        raise AssertionError("not called")

    proc = type("P", (), {"returncode": 1, "stderr": "boom"})()
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(github.GitHubError, match="boom"):
        github.get_issue("company/backend", 7)
