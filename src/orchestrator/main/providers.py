"""Concrete provider registries and factories for the composition root."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from orchestrator.application.ports import *  # noqa: F403
from orchestrator.domain import Context, PublishedChange, PublishedReview, PublishedTriage, ReviewOutcome, ReviewTarget, TriageOutcome, TriageTarget


class UnknownProviderError(ValueError):
    pass


ProviderFactory = Callable[[dict[str, Any]], object]


class ProviderRegistry:
    def __init__(self, providers: dict[str, ProviderFactory], boundary: type | None = None) -> None:
        self._providers, self._boundary = dict(providers), boundary

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def get(self, provider_type: str) -> ProviderFactory:
        try:
            return self._providers[provider_type]
        except KeyError as exc:
            raise UnknownProviderError(f"unknown provider type: {provider_type}") from exc

    def create(self, provider_type: str, options: dict[str, Any] | None = None) -> object:
        provider = self.get(provider_type)(dict(options or {}))
        if self._boundary is not None and not isinstance(provider, self._boundary):
            raise TypeError(f"provider factory {provider_type} returned an invalid implementation")
        return provider


@dataclass
class _PlaceholderInputSource:
    options: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> list[InputEvent]:
        return []


@dataclass
class _PlaceholderExecutor:
    options: dict[str, Any] = field(default_factory=dict)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(False, 1, stderr="executor provider is not wired", context=request.context.merge_namespace("placeholder", {"options": self.options}))


@dataclass
class _PlaceholderWorkspaceManager:
    options: dict[str, Any] = field(default_factory=dict)

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        return WorkspaceResult("", request.branch, request.context.merge_namespace("placeholder", {"options": self.options}))

    def cleanup(self, result: WorkspaceResult) -> None:
        return None


@dataclass
class _PlaceholderDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, request):
        return PublishedChange(provider="placeholder", context=request.context)


def _input_factory(options):
    from orchestrator.infra.github import auth as github_auth

    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderInputSource(options)
    from orchestrator.infra.github.client import GitHubClient
    from orchestrator.infra.github.input import GitHubPollingInputSource, GitHubSourceFeedback

    config_module = options.pop("_config_module")
    options.pop("auth", None)
    source = GitHubPollingInputSource(
        github_client=GitHubClient(identity), options=options, config_module=config_module,
    )
    source.feedback = GitHubSourceFeedback(source.github_client)
    return source


def _opencode_executor_factory(options):
    if not options.pop("_runtime", False):
        return _PlaceholderExecutor(options)
    from orchestrator.infra.opencode.executor import OpenCodeExecutor

    return OpenCodeExecutor(options=options)


def _codex_executor_factory(options):
    if not options.pop("_runtime", False):
        return _PlaceholderExecutor(options)
    from orchestrator.infra.codex.executor import CodexExecutor

    return CodexExecutor(options=options)


def _claude_executor_factory(options):
    if not options.pop("_runtime", False):
        return _PlaceholderExecutor(options)
    from orchestrator.infra.claude.executor import ClaudeExecutor

    return ClaudeExecutor(options=options)


def _workspace_factory(options):
    from orchestrator.infra.github import auth as github_auth

    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderWorkspaceManager(options)
    from orchestrator.infra.git.client import GitClient
    from orchestrator.infra.git.workspace import GitWorkspaceManager

    options.pop("auth", None)
    return GitWorkspaceManager(options=options, git_client=GitClient(identity))


def _destination_factory(options):
    from orchestrator.infra.github import auth as github_auth

    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderDestination(options)
    from orchestrator.infra.github.client import GitHubClient
    from orchestrator.infra.github.destination import GitHubDestination

    options.pop("auth", None)
    return GitHubDestination(options=options, github_client=GitHubClient(identity))


INPUT_PROVIDERS = ProviderRegistry({"github_polling": _input_factory}, InputSource)
EXECUTOR_PROVIDERS = ProviderRegistry({
    "claude": _claude_executor_factory,
    "codex": _codex_executor_factory,
    "opencode": _opencode_executor_factory,
}, Executor)
WORKSPACE_PROVIDERS = ProviderRegistry({"git": _workspace_factory}, WorkspaceManager)
DESTINATION_PROVIDERS = ProviderRegistry({"github": _destination_factory}, Destination)


@dataclass
class _PlaceholderReviewInputSource:
    options: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> list[ReviewTarget]:
        return []


@dataclass
class _PlaceholderReviewExecutor:
    options: dict[str, Any] = field(default_factory=dict)

    def execute(self, request: ReviewRequest) -> ReviewOutcome:
        return ReviewOutcome(False, summary="review executor provider is not wired", context=request.context)


@dataclass
class _PlaceholderReviewDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, target, outcome):
        return PublishedReview(provider="placeholder", context=target.context)


@dataclass
class _PlaceholderTriageInputSource:
    options: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> list[TriageTarget]:
        return []


@dataclass
class _PlaceholderTriageExecutor:
    options: dict[str, Any] = field(default_factory=dict)

    def execute(self, request: TriageRequest) -> TriageOutcome:
        return TriageOutcome(False, summary="triage executor provider is not wired", context=request.context)


@dataclass
class _PlaceholderTriageDestination:
    options: dict[str, Any] = field(default_factory=dict)

    def publish(self, target, outcome):
        return PublishedTriage(provider="placeholder", context=target.context)


def _triage_input_factory(options):
    from orchestrator.infra.github import auth as github_auth
    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderTriageInputSource(options)
    from orchestrator.infra.github.client import GitHubClient
    from orchestrator.infra.github.triage import GitHubTriageInputSource
    config_module = options.pop("_config_module")
    options.pop("auth", None)
    return GitHubTriageInputSource(GitHubClient(identity), config_module=config_module, options=options)


def _triage_executor_factory(provider_name, class_name):
    def factory(options):
        if not options.pop("_runtime", False):
            return _PlaceholderTriageExecutor(options)
        module = __import__(f"orchestrator.infra.{provider_name}.executor", fromlist=[class_name])
        return getattr(module, class_name)(options=options)
    return factory


def _triage_destination_factory(options):
    from orchestrator.infra.github import auth as github_auth
    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderTriageDestination(options)
    from orchestrator.infra.github.client import GitHubClient
    from orchestrator.infra.github.triage import GitHubTriageDestination
    options.pop("auth", None)
    return GitHubTriageDestination(
        options=options,
        github_client=GitHubClient(identity),
    )


TRIAGE_INPUT_PROVIDERS = ProviderRegistry({"github_polling": _triage_input_factory}, TriageInputSource)
TRIAGE_EXECUTOR_PROVIDERS = ProviderRegistry({
    "claude": _triage_executor_factory("claude", "ClaudeTriageExecutor"),
    "codex": _triage_executor_factory("codex", "CodexTriageExecutor"),
    "opencode": _triage_executor_factory("opencode", "OpenCodeTriageExecutor"),
}, TriageExecutor)
TRIAGE_DESTINATION_PROVIDERS = ProviderRegistry({"github": _triage_destination_factory}, TriageDestination)


def _review_input_factory(options):
    from orchestrator.infra.github import auth as github_auth

    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderReviewInputSource(options)
    from orchestrator.infra.github.client import GitHubClient
    from orchestrator.infra.github.review import GitHubReviewInputSource

    options.pop("store", None)
    config_module = options.pop("_config_module")
    options.pop("auth", None)
    return GitHubReviewInputSource(
        github_client=GitHubClient(identity), options=options, config_module=config_module,
    )


def _opencode_review_executor_factory(options):
    if not options.pop("_runtime", False):
        return _PlaceholderReviewExecutor(options)
    from orchestrator.infra.opencode.executor import OpenCodeReviewExecutor

    return OpenCodeReviewExecutor(options=options)


def _codex_review_executor_factory(options):
    if not options.pop("_runtime", False):
        return _PlaceholderReviewExecutor(options)
    from orchestrator.infra.codex.executor import CodexReviewExecutor

    return CodexReviewExecutor(options=options)


def _claude_review_executor_factory(options):
    if not options.pop("_runtime", False):
        return _PlaceholderReviewExecutor(options)
    from orchestrator.infra.claude.executor import ClaudeReviewExecutor

    return ClaudeReviewExecutor(options=options)


def _review_destination_factory(options):
    from orchestrator.infra.github import auth as github_auth

    identity = github_auth.identity_from_options(options)
    if not options.pop("_runtime", False):
        return _PlaceholderReviewDestination(options)
    from orchestrator.infra.github.client import GitHubClient
    from orchestrator.infra.github.review import GitHubReviewDestination

    options.pop("auth", None)
    return GitHubReviewDestination(options=options, github_client=GitHubClient(identity))


REVIEW_INPUT_PROVIDERS = ProviderRegistry({"github_polling": _review_input_factory}, ReviewInputSource)
REVIEW_EXECUTOR_PROVIDERS = ProviderRegistry({
    "claude": _claude_review_executor_factory,
    "codex": _codex_review_executor_factory,
    "opencode": _opencode_review_executor_factory,
}, ReviewExecutor)
REVIEW_WORKSPACE_PROVIDERS = ProviderRegistry({"git": _workspace_factory}, WorkspaceManager)
REVIEW_DESTINATION_PROVIDERS = ProviderRegistry({"github": _review_destination_factory}, ReviewDestination)
