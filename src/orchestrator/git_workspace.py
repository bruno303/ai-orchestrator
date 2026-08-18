"""Git-backed implementation of the workspace provider boundary."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from orchestrator import github, git, workspace
from orchestrator.providers import WorkspaceRequest, WorkspaceResult


class GitWorkspaceManager:
    """Prepare isolated task worktrees and remove them after successful tasks."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})
        self.provider_type = "git"

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        provider_state = {**self.options, **request.provider_state}
        repository_url = provider_state.get("repository_url") or github.get_clone_url(request.repository)
        base_branch = request.base_branch or github.get_default_branch(request.repository)
        repo_dir = git.ensure_base_clone(request.repository, repository_url)
        if not base_branch:
            base_branch = git.detect_default_branch(repo_dir)

        issue_number = request.task_id.rsplit("#", 1)[-1]
        branch = request.branch or f"ai/issue-{issue_number}"
        workspace_path = Path(
            provider_state.get("workspace")
            or workspace.task_workspace(request.repository, int(issue_number))
        )
        git.create_worktree(repo_dir, workspace_path, branch, base_branch)
        return WorkspaceResult(
            workspace=str(workspace_path),
            branch=branch,
            provider_state={
                "repository": request.repository,
                "repository_url": repository_url,
                "base_branch": base_branch,
                "repo_dir": str(repo_dir),
            },
        )

    def cleanup(self, result: WorkspaceResult) -> None:
        provider_state = result.provider_state
        repo_dir = Path(provider_state.get("repo_dir") or git.base_repo_dir(provider_state["repository"]))
        git.remove_worktree(repo_dir, Path(result.workspace), result.branch)
        if Path(result.workspace).exists():
            shutil.rmtree(Path(result.workspace), ignore_errors=True)
