"""Tests for the provider-neutral pull-request review workflow."""

import json
from types import SimpleNamespace

from orchestrator.main import config
from orchestrator.infra.github import client as github
from orchestrator.infra.github.review import GitHubReviewDestination, GitHubReviewInputSource
from orchestrator.infra.opencode.executor import OpenCodeResult, OpenCodeReviewExecutor
from orchestrator.domain import Context, ReviewCheck, ReviewFinding, ReviewOutcome, ReviewTarget
from orchestrator.application.ports import ReviewRequest
from orchestrator.application.review import ReviewApplication
from orchestrator.main.composition import compose_review_runtime


class FakeWorkspace:
    def prepare(self, request):
        return type("Workspace", (), {"workspace": "/tmp/review", "branch": "", "context": Context()})()

    def cleanup(self, result):
        self.cleaned = True


class FakeInput:
    def __init__(self, event):
        self.event = event

    def poll(self):
        return [self.event]


class FakeExecutor:
    def __init__(self, result):
        self.result = result

    def execute(self, request):
        return self.result


class FakeDestination:
    def __init__(self):
        self.published = []

    def publish(self, request, result):
        self.published.append((request, result))


def test_review_application_cleans_up_publishes_and_logs_completion(capsys):
    review_event = ReviewTarget(
        "review:company/backend#4", "company/backend", source_ref="feature",
        target_ref="main", revision="abc", context=Context({"github": {"pr_number": 4}}),
    )
    destination = FakeDestination()
    app = ReviewApplication(FakeInput(review_event), FakeExecutor(ReviewOutcome(True, verdict="approve")), FakeWorkspace(), destination)
    assert app.poll_once() == [review_event]
    assert destination.published[0][0].id == "review:company/backend#4"
    assert "[review] finished: repository=company/backend id=review:company/backend#4" in capsys.readouterr().out


def test_github_destination_labels_only_after_publication(monkeypatch):
    calls = []

    class GitHub:
        def publish_pull_request_review(self, *args, **kwargs):
            calls.append("review")

        def add_pull_request_label(self, *args, **kwargs):
            calls.append("label")

    target = ReviewTarget("review:company/backend#4", "company/backend", context=Context({"github": {"pr_number": 4, "changed_files": ["a.py"]}}))
    GitHubReviewDestination(github_client=GitHub()).publish(
        target, ReviewOutcome(True, verdict="comment", summary="ok", findings=(ReviewFinding("fix", path="a.py", line=2),), context=target.context)
    )
    assert calls == ["review", "label"]


def test_github_destination_comments_on_self_authored_pr():
    published = []

    class GitHub:
        def get_authenticated_user_login(self):
            return "app/bruno303-ai-agent-bot"

        def publish_pull_request_review(self, *args, **kwargs):
            published.append((args, kwargs))

        def add_pull_request_label(self, *args, **kwargs):
            pass

    target = ReviewTarget(
        "review:r#1", "r", revision="sha",
        context=Context({"github": {"pr_number": 1, "author_login": "app/bruno303-ai-agent-bot"}}),
    )
    GitHubReviewDestination(github_client=GitHub()).publish(
        target, ReviewOutcome(True, verdict="request_changes", summary="needs work", context=target.context)
    )

    assert published[0][0][4] == "COMMENT"


def test_github_destination_does_not_label_on_publication_failure():
    class GitHub:
        def publish_pull_request_review(self, *args, **kwargs):
            raise RuntimeError("failed")

        def add_pull_request_label(self, *args, **kwargs):
            raise AssertionError("label must not be added")

    target = ReviewTarget("review:r#1", "r", context=Context({"github": {"pr_number": 1}}))
    try:
        GitHubReviewDestination(github_client=GitHub()).publish(target, ReviewOutcome(True, verdict="comment", context=target.context))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected publication failure")


def test_review_poll_continues_after_cleanup_failure():
    events = [
        ReviewTarget(f"review:r#{number}", "r", context=Context({"github": {"pr_number": number}}))
        for number in (1, 2)
    ]

    class Workspace(FakeWorkspace):
        def prepare(self, request):
            return type("Workspace", (), {"workspace": "/tmp/review", "branch": "", "context": Context()})()

        def cleanup(self, result):
            raise RuntimeError("cleanup failed")

    destination = FakeDestination()
    app = ReviewApplication(type("Input", (), {"poll": lambda self: events})(), FakeExecutor(ReviewOutcome(True, verdict="comment")), Workspace(), destination)
    assert app.poll_once() == events
    assert len(destination.published) == 2


def test_review_runtime_uses_registries_without_store(monkeypatch):
    pipeline = config.ReviewPipelineConfig(
        config.ProviderConfig("input", {"input-option": 1}),
        config.ProviderConfig("executor", {"executor-option": 2}),
        config.ProviderConfig("workspace", {"workspace-option": 3}),
        config.ProviderConfig("destination", {"destination-option": 4}),
    )
    monkeypatch.setattr(config, "load_review_pipeline_config", lambda: pipeline)
    captured = []

    class Registry:
        def create(self, provider_type, options):
            captured.append((provider_type, options))
            return {
                "input": type("Input", (), {"poll": lambda self: []})(),
                "executor": type("Executor", (), {"execute": lambda self, request: ReviewResult(False)})(),
                    "workspace": type("Workspace", (), {"prepare": lambda self, request: None, "cleanup": lambda self, result: None})(),
                "destination": type("Destination", (), {"publish": lambda self, request, result: None})(),
            }[provider_type]

    import orchestrator.main.composition as composition
    for name in ("REVIEW_INPUT_PROVIDERS", "REVIEW_EXECUTOR_PROVIDERS", "REVIEW_WORKSPACE_PROVIDERS", "REVIEW_DESTINATION_PROVIDERS"):
        monkeypatch.setattr(composition, name, Registry())
    compose_review_runtime()
    assert all("store" not in options for _, options in captured)
    assert [provider for provider, _ in captured] == ["input", "executor", "workspace", "destination"]


def test_github_input_skips_broken_pr_metadata_and_continues():
    class Client:
        def list_open_pull_requests(self, repository):
            return [SimpleNamespace(number=1), SimpleNamespace(number=2)]

        def get_pull_request(self, repository, number):
            if number == 1:
                raise github.GitHubError("metadata unavailable")
            return github.PullRequestDetail(2, "title", "body", "url", "main", "head", [("a.py", "modified")])

    source = GitHubReviewInputSource(Client(), SimpleNamespace(allowed_repositories=lambda: ["r"]))
    events = source.poll()
    assert [event.context.namespace("github")["pr_number"] for event in events] == [2]


def test_github_review_input_uses_https_repository_url():
    class Client:
        def get_repository(self, repository):
            return {"ssh_url": "git@github.com:company/backend.git"}

        def https_clone_url(self, metadata, repository):
            return github.https_clone_url(metadata, repository)

        def list_open_pull_requests(self, repository):
            return []

    source = GitHubReviewInputSource(Client(), SimpleNamespace(allowed_repositories=lambda: ["company/backend"]))

    assert source._repository_state("company/backend") == {
        "repository_url": "https://github.com/company/backend.git"
    }


def test_github_destination_demotes_invalid_locations_and_validates_start_side():
    published = []

    class Client:
        def publish_pull_request_review(self, *args, **kwargs):
            published.append(args)

        def add_pull_request_label(self, *args):
            pass

    context = Context({"github": {"pr_number": 1, "changed_files": ["a.py"], "changed_lines": {"a.py": {"RIGHT": [2], "LEFT": []}}}})
    target = ReviewTarget("review:r#1", "r", context=context)
    result = ReviewOutcome(True, verdict="comment", findings=(
        ReviewFinding("valid", path="a.py", line=2),
        ReviewFinding("invalid", path="a.py", line=3),
        ReviewFinding("bad range", path="a.py", line=2, start_line=2, start_side="LEFT"),
    ), context=context)
    GitHubReviewDestination(github_client=Client()).publish(target, result)
    assert [comment["line"] for comment in published[0][3]] == [2]
    assert "invalid" in published[0][2]
    assert "bad range" in published[0][2]


def test_review_executor_rejects_invalid_structured_result(monkeypatch):
    monkeypatch.setattr("orchestrator.infra.opencode.executor.run_opencode", lambda *args, **kwargs: OpenCodeResult(0, json.dumps({
        "verdict": "comment", "summary": "x", "findings": [{"message": "x", "side": "middle"}], "checks": [],
    }), "", 0.1))
    result = OpenCodeReviewExecutor().execute(ReviewRequest("review:r#1", "r", "/tmp", "p", Context()))
    assert not result.success
    assert "invalid structured" in result.summary


def test_review_executor_uses_configured_review_model(monkeypatch):
    captured = {}

    def run(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return OpenCodeResult(0, json.dumps({
            "verdict": "comment", "summary": "ok", "findings": [], "checks": [],
        }), "", 0.1)

    monkeypatch.setattr("orchestrator.infra.opencode.executor.run_opencode", run)
    result = OpenCodeReviewExecutor({"model_config": config.ModelConfig("provider/model", "fast")}).execute(
        ReviewRequest("review:r#1", "r", "/tmp", "p", Context())
    )

    assert result.success
    assert captured["args"][1] is None
    assert captured["model"] == "provider/model"
    assert captured["variant"] == "fast"


def test_review_executor_accepts_transcript_before_json(monkeypatch):
    transcript = "agent output\n\x1b[0m\n" + json.dumps({
        "verdict": "approve", "summary": "ok",
        "findings": [{"message": "nested finding"}],
        "checks": [{"name": "tests", "status": "pass"}],
    })
    monkeypatch.setattr(
        "orchestrator.infra.opencode.executor.run_opencode",
        lambda *args, **kwargs: OpenCodeResult(0, transcript, "", 0.1),
    )

    result = OpenCodeReviewExecutor().execute(ReviewRequest("review:r#1", "r", "/tmp", "p", Context()))

    assert result.success
    assert result.verdict == "approve"


def test_review_executor_uses_configured_model(monkeypatch):
    captured = {}

    def run(*args, **kwargs):
        captured.update(kwargs)
        return OpenCodeResult(0, json.dumps({
            "verdict": "comment", "summary": "ok", "findings": [], "checks": [],
        }), "", 0.1)

    monkeypatch.setattr("orchestrator.infra.opencode.executor.run_opencode", run)
    result = OpenCodeReviewExecutor({"model_config": config.ModelConfig("provider/model", "fast")}).execute(
        ReviewRequest("review:r#1", "r", "/tmp", "p", Context())
    )

    assert result.success
    assert captured["model"] == "provider/model"
    assert captured["variant"] == "fast"
