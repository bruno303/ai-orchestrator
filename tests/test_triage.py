"""Tests for issue triage contracts and GitHub behavior."""

from types import SimpleNamespace

import pytest

from orchestrator.application.ports import TriageRequest
from orchestrator.application.triage import TriageApplication
from orchestrator.domain import Context, TriageOutcome, TriageTarget
from orchestrator.infra.github.client import Issue
from orchestrator.infra.github.triage import GitHubTriageDestination, GitHubTriageInputSource
from orchestrator.infra.opencode.executor import OpenCodeResult, OpenCodeTriageExecutor
from orchestrator.infra.triage.parser import parse_triage_output


def target(number=1, labels=()):
    return TriageTarget(
        f"triage:owner/repo#{number}", "owner/repo", "Title", "Description",
        context=Context({"github": {"issue_number": number, "url": f"https://example/{number}", "labels": list(labels)}}),
    )


def outcome(ready=False):
    return TriageOutcome(True, ready, "high" if ready else "medium", "summary", () if ready else ("acceptance criteria",))


def test_triage_parser_accepts_mixed_output_and_normalizes_confidence():
    result = parse_triage_output(
        'thinking\n{"enough_context":false,"confidence":"MEDIUM","summary":"need details","missing_context":["scope"]}',
        Context({"github": {"issue_number": 1}}),
    )

    assert result.success
    assert result.confidence == "medium"
    assert result.missing_context == ("scope",)


@pytest.mark.parametrize("payload", [
    '{"enough_context":"yes","confidence":"high","summary":"x","missing_context":[]}',
    '{"enough_context":true,"confidence":"certain","summary":"x","missing_context":[]}',
    '{"enough_context":true,"confidence":"high","summary":"x","missing_context":"none"}',
    "not json",
])
def test_triage_parser_rejects_invalid_output(payload):
    result = parse_triage_output(payload, Context())
    assert not result.success
    assert result.confidence == ""
    assert "invalid structured triage output" in result.summary


def test_github_triage_source_filters_workflow_labels_without_assigning():
    class Client:
        class GitHubError(Exception):
            pass

        def list_open_issues(self, repository):
            return [
                Issue(1, "ready", "body", "url", []),
                Issue(2, "agent", "body", "url", ["ai-agent"]),
                Issue(3, "blocked", "body", "url", ["ai-triage"]),
                Issue(4, "developed", "body", "url", ["ai-developed"]),
            ]

    source = GitHubTriageInputSource(Client(), config_module=SimpleNamespace(
        allowed_repositories=lambda: ["owner/repo"],
    ))

    assert [item.id for item in source.poll()] == ["triage:owner/repo#1"]


def test_github_triage_source_uses_repository_ready_label():
    class Client:
        class GitHubError(Exception):
            pass

        def list_open_issues(self, repository):
            return [
                Issue(1, "ready", "body", "url", ["ready-for-dev"]),
                Issue(2, "pending", "body", "url", []),
            ]

    source = GitHubTriageInputSource(Client(), config_module=SimpleNamespace(
        allowed_repositories=lambda: ["owner/repo"],
        repository_label=lambda _repository: "ready-for-dev",
    ))

    assert [item.id for item in source.poll()] == ["triage:owner/repo#2"]


def test_github_triage_destination_adds_agent_only_when_ready():
    class Client:
        def __init__(self): self.calls = []
        def add_issue_label(self, *args): self.calls.append(("add", *args))
        def remove_issue_label(self, *args): self.calls.append(("remove", *args))

    client = Client()
    GitHubTriageDestination(github_client=client).publish(target(), outcome(True))

    assert client.calls == [
        ("add", "owner/repo", 1, "ai-agent"),
        ("remove", "owner/repo", 1, "ai-triage"),
    ]


def test_github_triage_destination_uses_repository_ready_label():
    class Client:
        def __init__(self): self.calls = []
        def add_issue_label(self, *args): self.calls.append(("add", *args))
        def remove_issue_label(self, *args): self.calls.append(("remove", *args))

    client = Client()
    GitHubTriageDestination(
        github_client=client,
        config_module=SimpleNamespace(repository_label=lambda _repository: "ready-for-dev"),
    ).publish(target(), outcome(True))

    assert client.calls == [
        ("add", "owner/repo", 1, "ready-for-dev"),
        ("remove", "owner/repo", 1, "ai-triage"),
    ]


def test_github_triage_destination_comments_before_blocking_label():
    class Client:
        def __init__(self): self.calls = []
        def list_issue_comments(self, *args): return []
        def add_issue_comment(self, *args): self.calls.append(("comment", *args))
        def add_issue_label(self, *args): self.calls.append(("label", *args))

    client = Client()
    GitHubTriageDestination(github_client=client).publish(target(), outcome())

    assert [call[0] for call in client.calls] == ["comment", "label"]
    assert "**Confidence:** medium" in client.calls[0][3]
    assert "- acceptance criteria" in client.calls[0][3]


def test_github_triage_destination_does_not_label_when_comment_fails():
    class Client:
        def list_issue_comments(self, *args): return []
        def add_issue_comment(self, *args): raise RuntimeError("comment failed")
        def add_issue_label(self, *args): raise AssertionError("label must not be added")

    with pytest.raises(RuntimeError, match="comment failed"):
        GitHubTriageDestination(github_client=Client()).publish(target(), outcome())


def test_github_triage_destination_does_not_duplicate_identical_comment():
    class Client:
        def __init__(self): self.comments = []
        def list_issue_comments(self, *args): return [SimpleNamespace(body=self.comments[0])] if self.comments else []
        def add_issue_comment(self, *args): self.comments.append(args[2])
        def add_issue_label(self, *args): pass

    client = Client()
    destination = GitHubTriageDestination(github_client=client)
    destination.publish(target(), outcome())
    destination.publish(target(), outcome())

    assert len(client.comments) == 1


def test_triage_application_isolates_failed_targets():
    targets = [target(1), target(2)]
    published = []

    class Source:
        def poll(self): return targets

    class Executor:
        def execute(self, request):
            if request.task_id.endswith("#1"):
                raise RuntimeError("agent failed")
            return outcome(True)

    class Destination:
        def publish(self, target, result): published.append(target.id)

    processed = TriageApplication(Source(), Executor(), Destination()).poll_once()

    assert [item.id for item in processed] == [targets[1].id]
    assert published == [targets[1].id]


def test_opencode_triage_uses_model_and_parses_result(monkeypatch, tmp_path):
    captured = {}

    def run(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return OpenCodeResult(0, '{"enough_context":true,"confidence":"high","summary":"ok","missing_context":[]}', "", 0.1)

    monkeypatch.setattr("orchestrator.infra.opencode.executor.run_opencode", run)
    result = OpenCodeTriageExecutor({"model_config": SimpleNamespace(name="model/x", variant="fast")}).execute(
        TriageRequest("triage:r#1", "r", str(tmp_path), "assess", Context())
    )

    assert result.ready
    assert captured["args"][1] is None
    assert captured["model"] == "model/x"
    assert captured["variant"] == "fast"
