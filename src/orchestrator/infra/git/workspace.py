"""Git-backed implementation of the workspace provider boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.application.ports import WorkspaceRequest, WorkspaceResult
from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git
from orchestrator.infra.github import auth as github_auth


class GitWorkspaceManager:
    """Prepare isolated self-contained Git clones for task execution."""

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        git_client: Any | None = None,
    ) -> None:
        self.options = dict(options or {})
        self.git_client = git_client or git.GitClient(
            github_auth.identity_from_options(self.options)
        )
        self.provider_type = "git"

    def prepare(self, request: WorkspaceRequest) -> WorkspaceResult:
        git_context = {**self.options, **dict(request.context.namespace("git"))}
        repository_url = (
            request.repository_url
            or git_context.get("base_repository_url")
            or git_context.get("repository_url")
        )
        if not repository_url:
            raise git.GitError("workspace request requires a repository URL")

        base_branch = (
            request.base_branch
            or request.target_ref
            or git_context.get("base_branch", "")
        )
        repo_dir = self.git_client.ensure_base_clone(
            request.repository, repository_url
        )
        if not base_branch:
            base_branch = self.git_client.detect_default_branch(repo_dir)

        if request.purpose not in {"execution", "review", "discussion"}:
            raise git.GitError(f"unknown workspace purpose: {request.purpose}")

        review = (
            request.checkout_mode == "revision"
            or request.purpose in {"review", "discussion"}
        )
        branch = "" if review else request.branch or git_context.get("branch", "")
        workspace_value = request.workspace or git_context.get("workspace")
        if not workspace_value:
            workspace_value = str(
                workspace.review_workspace(request.task_id)
                if review
                else workspace.task_workspace(request.task_id)
            )
        if not review and not branch:
            branch = f"ai/{workspace.safe_task_token(request.task_id)[:80]}"

        workspace_path = Path(workspace_value)

        if review:
            commit = request.revision or git_context.get("revision")
            if not commit and request.purpose == "discussion":
                commit = f"origin/{base_branch}"
            if not commit:
                raise git.GitError("revision workspace requires a commit revision")

            workspace.remove_workspace(workspace_path)
            self.git_client.clone_workspace(
                repo_dir, workspace_path, repository_url
            )
            if request.revision or git_context.get("revision"):
                self.git_client.fetch_commit(
                    workspace_path,
                    commit,
                    request.fetch_url
                    or git_context.get("fetch_url")
                    or "origin",
                )
            self.git_client.checkout_detached(workspace_path, commit)
        else:
            reusable = (
                request.reuse_workspace
                and workspace_path.exists()
                and (workspace_path / ".git").is_dir()
            )
            if not reusable:
                workspace.remove_workspace(workspace_path)
                self.git_client.clone_workspace(
                    repo_dir, workspace_path, repository_url
                )

                revision = request.revision or git_context.get("revision")
                fetch_url = request.fetch_url or git_context.get("fetch_url")
                if revision:
                    # PR execution begins at the recorded head, including fork
                    # commits, then attaches that immutable revision to the
                    # requested local branch.
                    self.git_client.fetch_commit(
                        workspace_path,
                        revision,
                        fetch_url or "origin",
                    )
                    self.git_client.checkout_branch(
                        workspace_path,
                        branch,
                        base_branch,
                        start_point=revision,
                    )
                else:
                    self.git_client.checkout_branch(
                        workspace_path,
                        branch,
                        base_branch,
                    )

        result_context = request.context.merge_namespace(
            "git",
            {
                **dict(request.context.namespace("git")),
                "repository": request.repository,
                "repository_url": repository_url,
                "base_branch": base_branch,
                "branch": branch,
                "workspace": str(workspace_path),
                "repo_dir": str(repo_dir),
                "review": review,
            },
        )
        return WorkspaceResult(
            workspace=str(workspace_path),
            branch=branch,
            context=result_context,
            base_branch=base_branch,
        )

    def cleanup(self, result: WorkspaceResult) -> None:
        workspace.remove_workspace(Path(result.workspace))
