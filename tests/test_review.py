"""Tests for the provider-neutral pull-request review workflow."""

import json
from types import SimpleNamespace

from orchestrator import config, github
from orchestrator.github_review import GitHubReviewDestination, GitHubReviewInputSource
from orchestrator.opencode import OpenCodeResult, OpenCodeReviewExecutor
from orchestrator.providers import ReviewEvent, ReviewRequest, ReviewResult
from orchestrator.review import ReviewApplication, compose_review_runtime


class FakeWorkspace:
    def prepare(self, request):
        return type("Workspace", (), {"workspace": "/tmp/review", "branch": "", "provider_state": {}})()

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


def test_review_application_cleans_up_and_publishes():
    review_event = ReviewEvent("review:company/backend#4", "company/backend", provider_state={"number": 4, "head_ref": "feature", "base_ref": "main", "head_sha": "abc"})
    destination = FakeDestination()
    app = ReviewApplication(FakeInput(review_event), FakeExecutor(ReviewResult(True, verdict="approve")), FakeWorkspace(), destination)
    assert app.poll_once() == [review_event]
    assert destination.published[0][0].task_id == "review:company/backend#4"


def test_github_destination_labels_only_after_publication(monkeypatch):
    calls = []

    class GitHub:
        def publish_pull_request_review(self, *args, **kwargs):
            calls.append("review")

        def add_pull_request_label(self, *args, **kwargs):
            calls.append("label")

    request = ReviewRequest("review:company/backend#4", "company/backend", "/tmp", "p", {"number": 4, "changed_files": ["a.py"]})
    GitHubReviewDestination(github_client=GitHub()).publish(
        request, ReviewResult(True, verdict="comment", summary="ok", provider_state={"findings": [{"path": "a.py", "line": 2, "message": "fix"}]})
    )
    assert calls == ["review", "label"]


def test_github_destination_does_not_label_on_publication_failure():
    class GitHub:
        def publish_pull_request_review(self, *args, **kwargs):
            raise RuntimeError("failed")

        def add_pull_request_label(self, *args, **kwargs):
            raise AssertionError("label must not be added")

    request = ReviewRequest("review:r#1", "r", "/tmp", "p", {"number": 1})
    try:
        GitHubReviewDestination(github_client=GitHub()).publish(request, ReviewResult(True, verdict="comment"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected publication failure")


def test_review_poll_continues_after_cleanup_failure():
    events = [
        ReviewEvent(f"review:r#{number}", "r", provider_state={"number": number})
        for number in (1, 2)
    ]

    class Workspace(FakeWorkspace):
        def prepare(self, request):
            return type("Workspace", (), {"workspace": "/tmp/review", "branch": "", "provider_state": {}})()

        def cleanup(self, result):
            raise RuntimeError("cleanup failed")

    destination = FakeDestination()
    app = ReviewApplication(type("Input", (), {"poll": lambda self: events})(), FakeExecutor(ReviewResult(True, verdict="comment")), Workspace(), destination)
    assert app.poll_once() == events
    assert len(destination.published) == 2


def test_review_runtime_uses_registries_and_store(monkeypatch):
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

    import orchestrator.providers as providers
    for name in ("REVIEW_INPUT_PROVIDERS", "REVIEW_EXECUTOR_PROVIDERS", "REVIEW_WORKSPACE_PROVIDERS", "REVIEW_DESTINATION_PROVIDERS"):
        monkeypatch.setattr(providers, name, Registry())
    compose_review_runtime(store="store")
    assert all(options["store"] == "store" for _, options in captured)
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
    assert [event.provider_state["number"] for event in events] == [2]


def test_github_destination_demotes_invalid_locations_and_validates_start_side():
    published = []

    class Client:
        def publish_pull_request_review(self, *args, **kwargs):
            published.append(args)

        def add_pull_request_label(self, *args):
            pass

    request = ReviewRequest(
        "review:r#1", "r", "/tmp", "p",
        {"number": 1, "changed_files": ["a.py"], "changed_lines": {"a.py": {"RIGHT": [2], "LEFT": []}}},
    )
    result = ReviewResult(True, verdict="comment", comments=[
        {"message": "valid", "path": "a.py", "line": 2},
        {"message": "invalid", "path": "a.py", "line": 3},
        {"message": "bad range", "path": "a.py", "line": 2, "start_line": 2, "start_side": "LEFT"},
    ])
    GitHubReviewDestination(github_client=Client()).publish(request, result)
    assert [comment["line"] for comment in published[0][3]] == [2]
    assert "invalid" in published[0][2]
    assert "bad range" in published[0][2]


def test_review_executor_rejects_invalid_structured_result(monkeypatch):
    monkeypatch.setattr("orchestrator.opencode.run_opencode", lambda *args, **kwargs: OpenCodeResult(0, json.dumps({
        "verdict": "comment", "summary": "x", "findings": [{"message": "x", "side": "middle"}], "checks": [],
    }), "", 0.1))
    result = OpenCodeReviewExecutor().execute(ReviewRequest("review:r#1", "r", "/tmp", "p", {"number": 1}))
    assert not result.success
    assert "invalid structured" in result.summary


def test_review_executor_uses_configured_primary_model(monkeypatch):
    captured = {}

    def run(*args, **kwargs):
        captured.update(kwargs)
        return OpenCodeResult(0, json.dumps({
            "verdict": "comment", "summary": "ok", "findings": [], "checks": [],
        }), "", 0.1)

    monkeypatch.setattr("orchestrator.opencode.run_opencode", run)
    monkeypatch.setattr(config, "MODEL_PRIMARY", config.ModelConfig("provider/model", "fast"))
    result = OpenCodeReviewExecutor().execute(ReviewRequest("review:r#1", "r", "/tmp", "p"))

    assert result.success
    assert captured["model"] == "provider/model"
    assert captured["variant"] == "fast"
