"""Git-backed implementation of the workspace provider boundary."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git
from orchestrator.infra.github import auth as github_auth
from orchestrator.domain import Context
from orchestrator.application.ports import WorkspaceRequest, WorkspaceResult


class GitWorkspaceManager:
    """Prepare isolated task worktrees and remove them after successful tasks."""

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        git_client: Any | None = None,
    ) -> None:
        self.options = dict(options or {})
        self.git_client = git_client or git.GitClient(github_auth.identity_from_options(self.options))
        self.provider_type = "git"

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        git_context = {**self.options, **dict(request.context.namespace("git"))}
        repository_url = request.repository_url or git_context.get("repository_url")
        if not repository_url:
            raise git.GitError("workspace request requires a repository URL")
        base_branch = request.base_branch or request.target_ref or git_context.get("base_branch", "")
        repo_dir = self.git_client.ensure_base_clone(request.repository, repository_url)
        if not base_branch:
            base_branch = self.git_client.detect_default_branch(repo_dir)

        if request.purpose not in {"execution", "review"}:
            raise git.GitError(f"unknown workspace purpose: {request.purpose}")
        review = request.checkout_mode == "revision" or request.purpose == "review"
        branch = "" if review else request.branch or git_context.get("branch", "")
        workspace_value = request.workspace or git_context.get("workspace")
        if not workspace_value:
            workspace_value = str(
                workspace.review_workspace(request.task_id)
                if review else workspace.task_workspace(request.task_id)
            )
        if not review and not branch:
            branch = f"ai/{workspace.safe_task_token(request.task_id)[:80]}"
        workspace_path = Path(workspace_value)
        if review:
            commit = request.revision or git_context.get("revision")
            if not commit:
                raise git.GitError("revision workspace requires a commit revision")
            self.git_client.fetch_commit(
                repo_dir,
                commit,
                request.fetch_url or git_context.get("fetch_url")
                or "origin",
            )
            self.git_client.create_detached_worktree(repo_dir, workspace_path, commit)
        else:
            self.git_client.create_worktree(repo_dir, workspace_path, branch, base_branch)
        result_context = request.context.merge_namespace("git", {
            **dict(request.context.namespace("git")),
            "repository": request.repository,
            "repository_url": repository_url,
            "base_branch": base_branch,
            "branch": branch,
            "workspace": str(workspace_path),
            "repo_dir": str(repo_dir),
            "review": review,
        })
        return WorkspaceResult(
            workspace=str(workspace_path),
            branch=branch,
            context=result_context,
            base_branch=base_branch,
        )

    def cleanup(self, result: WorkspaceResult) -> None:
        git_context = dict(result.context.namespace("git"))
        repo_dir = Path(git_context.get("repo_dir") or git.base_repo_dir(git_context["repository"]))
        self.git_client.remove_worktree(repo_dir, Path(result.workspace), result.branch)
        if Path(result.workspace).exists():
            shutil.rmtree(Path(result.workspace), ignore_errors=True)
