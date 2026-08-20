"""Git-backed implementation of the workspace provider boundary."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from orchestrator import git, workspace
from orchestrator.providers import WorkspaceRequest, WorkspaceResult


class GitWorkspaceManager:
    """Prepare isolated task worktrees and remove them after successful tasks."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.provider_type = "git"

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        provider_state = {**self.options, **request.provider_state}
        repository_url = request.repository_url or provider_state.get("repository_url")
        if not repository_url:
            raise git.GitError("workspace request requires a repository URL")
        base_branch = request.base_branch or request.target_ref or provider_state.get("base_branch", "")
        repo_dir = git.ensure_base_clone(request.repository, repository_url)
        if not base_branch:
            base_branch = git.detect_default_branch(repo_dir)

        if request.purpose not in {"execution", "review"}:
            raise git.GitError(f"unknown workspace purpose: {request.purpose}")
        review = request.checkout_mode == "revision" or request.purpose == "review"
        branch = "" if review else request.branch
        if not review and not branch:
            legacy_checkout = workspace.legacy_task_checkout(request.task_id, request.repository)
            if legacy_checkout is not None:
                branch = legacy_checkout[0]
        workspace_value = request.workspace or provider_state.get("workspace")
        if not workspace_value:
            # Legacy checkpoints predate explicit workspace instructions.
            legacy_checkout = workspace.legacy_task_checkout(request.task_id, request.repository)
            if request.purpose == "execution" and legacy_checkout is not None:
                workspace_value = str(legacy_checkout[1])
            else:
                raise git.GitError("workspace request requires a workspace path")
        workspace_path = Path(workspace_value)
        if review:
            commit = request.revision or provider_state.get("revision") or provider_state.get("head_sha")
            if not commit:
                raise git.GitError("revision workspace requires a commit revision")
            git.fetch_commit(
                repo_dir,
                commit,
                request.fetch_url or provider_state.get("fetch_url")
                or provider_state.get("head_clone_url") or "origin",
            )
            git.create_detached_worktree(repo_dir, workspace_path, commit)
        else:
            git.create_worktree(repo_dir, workspace_path, branch, base_branch)
        return WorkspaceResult(
            workspace=str(workspace_path),
            branch=branch,
            provider_state={
                "repository": request.repository,
                "repository_url": repository_url,
                "base_branch": base_branch,
                "repo_dir": str(repo_dir),
                "review": review,
            },
        )

    def cleanup(self, result: WorkspaceResult) -> None:
        provider_state = result.provider_state
        repo_dir = Path(provider_state.get("repo_dir") or git.base_repo_dir(provider_state["repository"]))
        git.remove_worktree(repo_dir, Path(result.workspace), result.branch)
        if Path(result.workspace).exists():
            shutil.rmtree(Path(result.workspace), ignore_errors=True)
