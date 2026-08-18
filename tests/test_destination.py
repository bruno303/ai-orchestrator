"""Tests for GitHub destination publication."""

from orchestrator import git
from orchestrator.github_destination import GitHubDestination
from orchestrator.providers import PublicationRequest


def test_destination_preserves_body_and_removes_existing_exact_closing_line():
    from orchestrator.github_destination import _body

    assert _body(12, {}, "Summary\n\nCloses #12\n\nDetails") == "Closes #12\n\nSummary\n\nDetails"


def test_destination_does_not_treat_prefixed_issue_number_as_a_closing_line():
    from orchestrator.github_destination import _body

    assert _body(12, {}, "Closes #123\n\nDetails") == "Closes #12\n\nCloses #123\n\nDetails"


def test_destination_replaces_duplicate_exact_closing_lines():
    from orchestrator.github_destination import _body

    body = "Summary\n\nCloses #12\n\nDetails\n\nCloses #12"
    assert _body(12, {}, body) == "Closes #12\n\nSummary\n\nDetails"


def test_graph_body_matches_issue_number_exactly_and_removes_duplicates():
    from orchestrator.graph import _pr_body

    state = {"input": {"data": {"number": 12}}}
    body = "Summary\n\nCloses #12\n\nCloses #123\n\nCloses #12\n\nDetails"
    assert _pr_body(state, body) == "Closes #12\n\nSummary\n\nCloses #123\n\nDetails"


def test_destination_reuses_existing_pr_without_changing_body(monkeypatch, tmp_path):
    calls: list[tuple] = []
    monkeypatch.setattr(git, "has_changes", lambda workspace: False)
    monkeypatch.setattr(git, "commits_ahead", lambda workspace, base: 1)
    monkeypatch.setattr(git, "push_branch", lambda workspace, branch: calls.append(("push", branch)))
    monkeypatch.setattr("orchestrator.github.find_open_pr", lambda repository, head: 7)
    existing_body = "Closes #1\n\nExisting PR description"
    monkeypatch.setattr(
        "orchestrator.github.get_pull_request",
        lambda repository, number: type("PR", (), {"body": existing_body})(),
    )
    monkeypatch.setattr(
        "orchestrator.github.update_pull_request_body",
        lambda repository, number, body: calls.append(("update", number, body)),
    )
    result = GitHubDestination().publish(
        PublicationRequest(
            "company/backend", "feat: x", "Closes #1", "ai/issue-1", "main",
            provider_state={
                "workspace": str(tmp_path), "issue_number": 1,
            },
        )
    )
    assert result.number == 7
    assert calls[0] == ("push", "ai/issue-1")
    assert calls == [("push", "ai/issue-1")]
