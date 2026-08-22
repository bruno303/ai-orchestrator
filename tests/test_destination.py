"""Tests for GitHub destination publication."""

from orchestrator.infra.git import client as git
from orchestrator.domain import ChangeRequest, Context, PublishedChange
from orchestrator.infra.github.destination import GitHubDestination


def test_destination_preserves_body_and_removes_existing_exact_closing_line():
    from orchestrator.infra.github.destination import _body

    assert _body(12, "Summary\n\nCloses #12\n\nDetails") == "Closes #12\n\nSummary\n\nDetails"


def test_destination_does_not_treat_prefixed_issue_number_as_a_closing_line():
    from orchestrator.infra.github.destination import _body

    assert _body(12, "Closes #123\n\nDetails") == "Closes #12\n\nCloses #123\n\nDetails"


def test_destination_replaces_duplicate_exact_closing_lines():
    from orchestrator.infra.github.destination import _body

    body = "Summary\n\nCloses #12\n\nDetails\n\nCloses #12"
    assert _body(12, body) == "Closes #12\n\nSummary\n\nDetails"


def test_github_body_matches_issue_number_exactly_and_removes_duplicates():
    from orchestrator.infra.github.destination import _body

    body = "Summary\n\nCloses #12\n\nCloses #123\n\nCloses #12\n\nDetails"
    assert _body(12, body) == "Closes #12\n\nSummary\n\nCloses #123\n\nDetails"


def test_destination_reuses_existing_pr_without_changing_body(monkeypatch, tmp_path):
    calls: list[tuple] = []
    monkeypatch.setattr(git, "has_changes", lambda workspace: False)
    monkeypatch.setattr(git, "commits_ahead", lambda workspace, base: 1)
    monkeypatch.setattr(git, "push_branch", lambda workspace, branch: calls.append(("push", branch)))
    monkeypatch.setattr("orchestrator.infra.github.client.find_open_pr", lambda repository, head: 7)
    existing_body = "Closes #1\n\nExisting PR description"
    monkeypatch.setattr(
        "orchestrator.infra.github.client.get_pull_request",
        lambda repository, number: type("PR", (), {"body": existing_body})(),
    )
    monkeypatch.setattr(
        "orchestrator.infra.github.client.update_pull_request_body",
        lambda repository, number, body: calls.append(("update", number, body)),
    )
    monkeypatch.setattr("orchestrator.infra.github.client.add_issue_label", lambda *args: calls.append(("label", *args)))
    result = GitHubDestination().publish(
        ChangeRequest(
            "company/backend#1", "company/backend", "feat: x", "Closes #1",
            "ai/issue-1", "main", Context({
                "github": {"issue_number": 1}, "git": {"workspace": str(tmp_path)},
            }),
        )
    )
    assert result.id == "7"
    assert calls[0] == ("push", "ai/issue-1")
    assert calls == [("push", "ai/issue-1"), ("label", "company/backend", 1, "ai-developed")]


def test_github_destination_reads_issue_metadata_from_its_namespace(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(git, "has_changes", lambda value: True)
    monkeypatch.setattr(git, "commits_ahead", lambda *args: 0)
    monkeypatch.setattr(git, "commit_all", lambda workspace, message: calls.append(("commit", message)))
    monkeypatch.setattr(git, "push_branch", lambda workspace, branch: calls.append(("push", branch)))
    monkeypatch.setattr("orchestrator.infra.github.client.find_open_pr", lambda *args: None)
    monkeypatch.setattr(
        "orchestrator.infra.github.client.create_pull_request",
        lambda repository, title, body, **kwargs: calls.append(("create", body, kwargs)) or 23,
    )
    monkeypatch.setattr("orchestrator.infra.github.client.add_issue_label", lambda *args: calls.append(("label", *args)))
    request = ChangeRequest(
        "company/backend#7", "company/backend", "feat: task", "description",
        "ai/issue-7", "main",
        Context({
            "github": {"issue_number": 7},
            "git": {"workspace": str(tmp_path)},
            "opencode": {"session_id": "s1"},
        }),
    )

    result = GitHubDestination().publish(request)

    assert isinstance(result, PublishedChange)
    assert result.id == "23"
    assert calls[0] == ("commit", "feat: task\n\nCloses #7")
    assert calls[2][1].startswith("Closes #7")


def test_destination_does_not_report_success_when_developed_label_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(git, "has_changes", lambda workspace: False)
    monkeypatch.setattr(git, "commits_ahead", lambda workspace, base: 1)
    monkeypatch.setattr(git, "push_branch", lambda *args: None)
    monkeypatch.setattr("orchestrator.infra.github.client.find_open_pr", lambda *args: 12)
    monkeypatch.setattr("orchestrator.infra.github.client.get_pull_request", lambda *args: type("PR", (), {"body": "Closes #1"})())
    monkeypatch.setattr("orchestrator.infra.github.client.add_issue_label", lambda *args: (_ for _ in ()).throw(RuntimeError("label failed")))
    import pytest
    with pytest.raises(RuntimeError, match="label failed"):
        GitHubDestination().publish(ChangeRequest("company/backend#1", "company/backend", "title", "",
            "ai/issue-1", "main", Context({"github": {"issue_number": 1}, "git": {"workspace": str(tmp_path)}})))
