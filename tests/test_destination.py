"""Tests for GitHub destination publication."""

from orchestrator import git
from orchestrator.github_destination import GitHubDestination
from orchestrator.providers import PublicationRequest


def test_destination_reuses_and_updates_existing_pr(monkeypatch, tmp_path):
    calls: list[tuple] = []
    monkeypatch.setattr(git, "has_changes", lambda workspace: False)
    monkeypatch.setattr(git, "commits_ahead", lambda workspace, base: 1)
    monkeypatch.setattr(git, "push_branch", lambda workspace, branch: calls.append(("push", branch)))
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda repository, head: 7)
    monkeypatch.setattr("orchestrator.github.get_pull_request", lambda repository, number: type("PR", (), {"body": "Closes #1"})())
    monkeypatch.setattr(
        "orchestrator.github.update_pull_request_body",
        lambda repository, number, body: calls.append(("update", number, body)),
    )
    result = GitHubDestination().publish(
        PublicationRequest(
            "company/backend", "feat: x", "Closes #1", "ai/issue-1", "main",
            provider_state={
                "workspace": str(tmp_path), "issue_number": 1,
                "review_verdict": "CHANGES_REQUIRED",
                "review_result": "FINDINGS:\n- Fix this",
            },
        )
    )
    assert result.number == 7
    assert calls[0] == ("push", "ai/issue-1")
    assert calls[1][0:2] == ("update", 7)
    assert "Fix this" in calls[1][2]
