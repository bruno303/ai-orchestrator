"""Tests for provider-neutral contracts and registries."""

from __future__ import annotations

import json

from orchestrator.application.ports import (
    Destination,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    InputEvent,
    InputSource,
    WorkspaceManager,
    WorkspaceRequest,
    WorkspaceResult,
    ReviewRequest,
    ReviewInputSource,
    ReviewExecutor,
    ReviewDestination,
)
from orchestrator.application import PollingApplication
from orchestrator.main.providers import (
    DESTINATION_PROVIDERS, EXECUTOR_PROVIDERS, INPUT_PROVIDERS,
    REVIEW_DESTINATION_PROVIDERS, REVIEW_EXECUTOR_PROVIDERS, REVIEW_INPUT_PROVIDERS,
    UnknownProviderError, WORKSPACE_PROVIDERS,
)
from orchestrator.main import config
from orchestrator.main.composition import compose_review_runtime, compose_runtime
from orchestrator.domain import Artifact, Context, ReviewOutcome, ReviewTarget, WorkItem
from orchestrator.domain import ChangeRequest, PublishedChange
from orchestrator.infra.github.destination import GitHubDestination
from orchestrator.infra.github.client import GitHubClient
from orchestrator.infra.github.input import GitHubPollingInputSource
from orchestrator.infra.git.workspace import GitWorkspaceManager
from orchestrator.infra.opencode.executor import OpenCodeExecutor
from orchestrator.infra.codex.executor import CodexExecutor, CodexReviewExecutor
from orchestrator.infra.claude.executor import ClaudeExecutor, ClaudeReviewExecutor


def test_provider_models_are_json_serializable():
    event = InputEvent(
        "issue-1", WorkItem("company/backend#1", "company/backend", "Fix bug",
                             context=Context({"github": {"issue_number": 1}})),
        "new", Context(), {},
    )
    request = ExecutionRequest("task-1", "/tmp/workspace", "do the work", "build")
    publication = ChangeRequest(
        "company/backend#1", "company/backend", "Fix bug", "Closes #1",
        "ai/issue-1", "main", Context(),
        artifacts=[Artifact("work.txt")],
    )

    encoded = json.dumps({"event": event.to_dict(), "request": request.to_dict(), "publication": publication.to_dict()})
    assert json.loads(encoded)["publication"]["artifacts"][0]["path"] == "work.txt"


def test_review_models_are_json_serializable():
    models = [
        ReviewTarget("review-1", "company/backend", context=Context({"github": {"pr_number": 14}})),
        ReviewRequest("task-1", "company/backend", "/tmp/review", "review it", Context({"github": {"pr_number": 14}})),
        ReviewOutcome(True, verdict="approve", findings=(), context=Context()),
    ]
    assert all(json.loads(json.dumps(model.to_dict())) for model in models)


def test_all_boundary_models_serialize_context():
    models = [
        ExecutionResult(True, 0, context=Context({"opencode": {"attempt": 1}})),
        WorkspaceRequest("task-1", "company/backend", "ai/task-1", "main", context=Context({"git": {"id": "w1"}})),
        WorkspaceResult("/tmp/workspace", "ai/task-1", Context({"git": {"id": "w1"}})),
        PublishedChange("pr-3", "https://example.test/3", "github", Context({"github": {"id": "pr-3"}})),
    ]
    assert all(json.loads(json.dumps(model.to_dict())) for model in models)


def test_model_rejects_non_json_context():
    try:
        Context({"opencode": {"bad": object()}})
    except TypeError as exc:
        assert "not JSON-serializable" in str(exc)
    else:
        raise AssertionError("expected serialization error")


def test_context_is_a_json_serializable_mapping():
    context = Context({"git": {"checkout": "main"}})
    assert context["git"]["checkout"] == "main"
    assert context.merged({"github": {"issue": 4}}) == {
        "git": {"checkout": "main"}, "github": {"issue": 4},
    }


def test_provider_registries_reserve_current_provider_names():
    assert INPUT_PROVIDERS.names() == ("github_polling",)
    assert EXECUTOR_PROVIDERS.names() == ("claude", "codex", "opencode")
    assert WORKSPACE_PROVIDERS.names() == ("git",)
    assert DESTINATION_PROVIDERS.names() == ("github",)
    assert REVIEW_INPUT_PROVIDERS.names() == ("github_polling",)
    assert REVIEW_EXECUTOR_PROVIDERS.names() == ("claude", "codex", "opencode")
    assert REVIEW_DESTINATION_PROVIDERS.names() == ("github",)


def test_review_registries_return_protocol_implementations():
    assert isinstance(REVIEW_INPUT_PROVIDERS.create("github_polling"), ReviewInputSource)
    assert isinstance(REVIEW_EXECUTOR_PROVIDERS.create("opencode"), ReviewExecutor)
    assert isinstance(REVIEW_EXECUTOR_PROVIDERS.create("codex"), ReviewExecutor)
    assert isinstance(REVIEW_EXECUTOR_PROVIDERS.create("claude"), ReviewExecutor)
    assert isinstance(REVIEW_DESTINATION_PROVIDERS.create("github"), ReviewDestination)


def test_unknown_provider_fails_clearly():
    try:
        INPUT_PROVIDERS.get("not-a-provider")
    except UnknownProviderError as exc:
        assert "unknown provider type: not-a-provider" in str(exc)
    else:
        raise AssertionError("expected UnknownProviderError")


def test_github_provider_auth_is_validated_for_each_provider():
    import pytest

    for registry, provider in (
        (INPUT_PROVIDERS, "github_polling"),
        (DESTINATION_PROVIDERS, "github"),
        (REVIEW_INPUT_PROVIDERS, "github_polling"),
        (REVIEW_DESTINATION_PROVIDERS, "github"),
    ):
        with pytest.raises(ValueError, match="'bot' or 'user'"):
            registry.create(provider, {"auth": "invalid"})


def test_runtime_github_providers_receive_independent_selected_clients(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n  execution:\n"
        "    input_source: {type: github_polling, auth: user}\n"
        "    destination: {type: github, auth: bot}\n"
        "  review:\n"
        "    input_source: {type: github_polling, auth: bot}\n"
        "    destination: {type: github, auth: user}\n"
    )
    config.load_pipeline_config.cache_clear()
    config.load_review_pipeline_config.cache_clear()

    execution = compose_runtime()
    reviews = compose_review_runtime()

    assert isinstance(execution.input_source.github_client, GitHubClient)
    assert execution.input_source.github_client.identity.mode == "user"
    assert execution.destination.github_client.identity.mode == "bot"
    assert reviews.input_source.github_client.identity.mode == "bot"
    assert reviews.runtime.destination.github_client.identity.mode == "user"


def test_registered_factories_return_protocol_implementations():
    input_source = INPUT_PROVIDERS.create("github_polling", {"interval": 30})
    executor = EXECUTOR_PROVIDERS.create("opencode")
    codex = EXECUTOR_PROVIDERS.create("codex")
    claude = EXECUTOR_PROVIDERS.create("claude")
    workspace = WORKSPACE_PROVIDERS.create("git")
    destination = DESTINATION_PROVIDERS.create("github")

    assert isinstance(input_source, InputSource)
    assert isinstance(executor, Executor)
    assert isinstance(codex, Executor)
    assert isinstance(claude, Executor)
    assert isinstance(workspace, WorkspaceManager)
    assert isinstance(destination, Destination)
    assert input_source.poll() == []
    assert isinstance(executor.execute(ExecutionRequest("t", "/tmp/w", "p", "a")), ExecutionResult)
    workspace_result = workspace.prepare(WorkspaceRequest("t", "r", "b", "main"))
    assert isinstance(workspace_result, WorkspaceResult)
    assert isinstance(destination.publish(ChangeRequest("t", "r", "t", "b", "h", "main", Context())), PublishedChange)


def test_compose_runtime_builds_concrete_providers_and_forwards_options(allowlist, tmp_path):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n"
        "  execution:\n"
        "    input_source:\n      type: github_polling\n      interval: 30\n"
        "    executor:\n      type: opencode\n      timeout: 10\n"
        "    workspace_manager:\n      type: git\n      root: /tmp/workspaces\n"
        "    destination:\n      type: github\n      draft: true\n"
    )
    config.load_pipeline_config.cache_clear()

    runtime = compose_runtime()

    assert isinstance(runtime.input_source, GitHubPollingInputSource)
    assert isinstance(runtime.executor, OpenCodeExecutor)
    assert isinstance(runtime.workspace_manager, GitWorkspaceManager)
    assert isinstance(runtime.destination, GitHubDestination)
    assert runtime.input_source.options == {"interval": 30}
    assert runtime.executor.options == {"timeout": 10}
    assert runtime.workspace_manager.options == {"root": "/tmp/workspaces"}
    assert runtime.destination.options == {"draft": True}


def test_compose_runtime_builds_codex_executor(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n  execution:\n    executor:\n      type: codex\n      timeout: 10\n"
    )
    config.load_pipeline_config.cache_clear()

    runtime = compose_runtime()

    assert isinstance(runtime.executor, CodexExecutor)
    assert runtime.executor.options == {"timeout": 10}


def test_compose_runtime_builds_claude_executor(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n  execution:\n    executor:\n      type: claude\n      permission_mode: acceptEdits\n"
    )
    config.load_pipeline_config.cache_clear()

    runtime = compose_runtime()

    assert isinstance(runtime.executor, ClaudeExecutor)
    assert runtime.executor.options == {"permission_mode": "acceptEdits"}


def test_compose_review_builds_codex_executor(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n  review:\n    executor:\n      type: codex\n"
    )
    config.load_pipeline_config.cache_clear()

    runtime = compose_review_runtime()

    assert isinstance(runtime.runtime.executor, CodexReviewExecutor)


def test_compose_review_builds_claude_executor(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n  review:\n    executor:\n      type: claude\n"
    )
    config.load_pipeline_config.cache_clear()

    runtime = compose_review_runtime()

    assert isinstance(runtime.runtime.executor, ClaudeReviewExecutor)


def test_composition_uses_independent_execution_and_review_models(allowlist):
    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "model:\n"
        "  execution: {name: provider/execution, variant: high}\n"
        "  review: {name: provider/review, variant: low}\n"
    )
    config.load_execution_model_config.cache_clear()
    config.load_review_model_config.cache_clear()

    execution = compose_runtime()
    reviews = compose_review_runtime()

    assert execution.execution_runtime.agent.settings.model == config.ModelConfig("provider/execution", "high")
    assert reviews.runtime.executor.options["model_config"] == config.ModelConfig("provider/review", "low")


def test_composed_github_source_records_configured_provider_name(allowlist, tmp_path):
    class FakeGitHub:
        class GitHubError(Exception):
            pass

        @staticmethod
        def list_open_issues(repository, label=None, assignee=None):
            from orchestrator.infra.github.client import Issue

            return [Issue(7, "Fix bug", "details", "https://example.test/7")]

        @staticmethod
        def assign_issue_to_authenticated_user(repository, number):
            return None

        @staticmethod
        def list_open_pull_requests(repository):
            return []

        @staticmethod
        def list_issue_comments(repository, number):
            return []

    config.CONFIG_FILE.write_text(
        "repositories:\n  - name: company/backend\n"
        "pipeline:\n  execution:\n    input_source:\n      type: github_polling\n"
    )
    config.load_pipeline_config.cache_clear()
    runtime = compose_runtime()
    runtime.input_source.github_client = FakeGitHub
    runtime.input_source.feedback.github_client = FakeGitHub
    seeds = []
    app = PollingApplication(
        runtime.input_source,
        lambda seed, task_id: seeds.append(seed) or {"task_id": task_id, "status": "FAILED"},
        lambda result: None,
        lambda *args: None,
    )

    app.poll_once(once=True)

    assert runtime.input_source.provider_type == "github_polling"
    assert seeds[0]["input"]["provider"] == "github_polling"
