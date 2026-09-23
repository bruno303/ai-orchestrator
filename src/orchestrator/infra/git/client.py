"""Git operations: base clones, worktrees, branches, commits, pushes."""

from __future__ import annotations

import shutil
import subprocess
import os
from contextvars import ContextVar
from pathlib import Path
from functools import wraps

from orchestrator.infra.github import auth as github_auth


def _environment_path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


REPOS_DIR = _environment_path("ORCHESTRATOR_REPOS_DIR", Path.home() / "agent-repos")


class GitError(Exception):
    pass


class NoChangesError(GitError):
    pass


_identity: ContextVar[github_auth.GitHubIdentity | None] = ContextVar("git_identity", default=None)


class GitClient:
    """An identity-bound view of the Git adapter.

    Module-level functions retain the bot-compatible default. Runtime workspace
    and publication adapters receive this client so the selected provider
    identity is carried through every GitHub remote operation.
    """

    def __init__(self, identity: github_auth.GitHubIdentity | None = None) -> None:
        self.identity = identity or github_auth.GitHubIdentity()

    def _call(self, function, *args, **kwargs):
        token = _identity.set(self.identity)
        try:
            return function(*args, **kwargs)
        finally:
            _identity.reset(token)

    def __getattr__(self, name: str):
        function = globals().get(name)
        if not callable(function):
            raise AttributeError(name)

        @wraps(function)
        def call(*args, **kwargs):
            return self._call(function, *args, **kwargs)

        return call


def _run(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitError(f"git not found: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc


def base_repo_dir(repository: str) -> Path:
    return REPOS_DIR / repository.replace("/", "-")


def ensure_base_clone(repository: str, clone_url: str) -> Path:
    """Clone the repository once into REPOS_DIR if not present, then fetch."""
    repo_dir = base_repo_dir(repository)
    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", clone_url, str(repo_dir)], cwd=repo_dir.parent,
             env=_github_env_for_url(clone_url))
    fetch(repo_dir)
    prune_worktrees(repo_dir)
    return repo_dir


def fetch(repo_dir: Path) -> None:
    _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir,
         env=_github_env_for_remote(repo_dir, "origin"))


def fetch_commit(repo_dir: Path, commit: str, remote: str = "origin") -> None:
    """Fetch an immutable commit, including one advertised by a fork remote."""
    environment = (
        _github_env_for_url(remote)
        if "github.com" in remote
        else _github_env_for_remote(repo_dir, remote)
    )
    _run(["git", "fetch", remote, commit], cwd=repo_dir,
         env=environment)


def detect_default_branch(repo_dir: Path) -> str:
    """Best-effort default branch detection (used for local repos without gh)."""
    proc = _run(
        ["git", "rev-parse", "--abbrev-ref", "refs/remotes/origin/HEAD"],
        cwd=repo_dir,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout.strip().removeprefix("origin/")
    proc = _run(["git", "branch", "-r"], cwd=repo_dir, check=False)
    branches = [line.strip() for line in proc.stdout.splitlines() if "HEAD" not in line]
    if branches:
        return branches[0].removeprefix("origin/")
    raise GitError(f"could not detect default branch in {repo_dir}")


def prune_worktrees(repo_dir: Path) -> None:
    """Drop registrations for worktrees whose directories are already gone.

    Without this, a worktree that was deleted outside `git worktree remove`
    leaves a stale entry in the base clone, and the next `worktree add` at the
    same path fails with "missing but already registered worktree".
    """
    if repo_dir.exists():
        _run(["git", "worktree", "prune"], cwd=repo_dir, check=False)


def remove_worktree(repo_dir: Path, workspace: Path, branch: str) -> None:
    """Remove a task worktree, its registration, and its local branch.

    Used by cleanup and clean re-runs. The worktree directory is always
    removed, the base clone is pruned for stale registrations, and any failure
    to delete the directory or branch is surfaced instead of silently leaving a
    residual repo behind.
    """
    if (
        repo_dir.exists()
        and (workspace.exists() or workspace.is_symlink())
        and ((workspace / ".git").exists() or (workspace / ".git").is_symlink())
    ):
        _run(["git", "worktree", "remove", "--force", str(workspace)], cwd=repo_dir, check=False)
    if workspace.exists() or workspace.is_symlink():
        if workspace.is_dir() and not workspace.is_symlink():
            shutil.rmtree(workspace)
        else:
            workspace.unlink()
    prune_worktrees(repo_dir)
    if branch and repo_dir.exists():
        proc = _run(["git", "branch", "--list", branch], cwd=repo_dir, check=False)
        if proc.returncode != 0:
            raise GitError(f"git branch --list failed for {branch}: {proc.stderr.strip()}")
        if branch in proc.stdout.split():
            proc = _run(["git", "branch", "-D", branch], cwd=repo_dir, check=False)
            if proc.returncode != 0:
                raise GitError(f"git branch -D failed for {branch}: {proc.stderr.strip()}")
    if workspace.exists() or workspace.is_symlink():
        raise GitError(f"workspace remains after removal: {workspace}")


def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    proc = _run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=repo_dir,
        check=False,
    )
    return proc.returncode == 0


def create_worktree(
    repo_dir: Path,
    workspace: Path,
    branch: str,
    base_branch: str,
    start_point: str | None = None,
) -> None:
    """Create an isolated worktree at `workspace` on `branch`.

    Reuses `origin/{branch}` when it already exists — a comment-triggered
    re-run on a task that already has an open PR should keep building on that
    branch, not silently discard it by forking a fresh one from base_branch.
    """
    if workspace.exists():
        raise GitError(f"workspace already exists: {workspace}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if start_point:
        proc = _run(
            ["git", "worktree", "add", "-B", branch, str(workspace), start_point],
            cwd=repo_dir,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"worktree add failed: {proc.stderr.strip()}")
        return
    if remote_branch_exists(repo_dir, branch):
        proc = _run(
            ["git", "worktree", "add", "-B", branch, str(workspace), f"origin/{branch}"],
            cwd=repo_dir,
            check=False,
        )
        if proc.returncode == 0:
            return
    base_ref = f"origin/{base_branch}"
    proc = _run(
        ["git", "worktree", "add", "-b", branch, str(workspace), base_ref],
        cwd=repo_dir,
        check=False,
    )
    if proc.returncode != 0:
        # Fallback for local repos without an origin remote layout.
        proc = _run(
            ["git", "worktree", "add", "-b", branch, str(workspace), base_branch],
            cwd=repo_dir,
            check=False,
        )
    if proc.returncode != 0:
        raise GitError(f"worktree add failed: {proc.stderr.strip()}")


def create_detached_worktree(repo_dir: Path, workspace: Path, commit: str) -> None:
    """Create an isolated, detached worktree at an immutable commit."""
    if workspace.exists():
        raise GitError(f"workspace already exists: {workspace}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "--detach", str(workspace), commit], cwd=repo_dir)


def commits_ahead(workspace: Path, base_branch: str) -> int:
    """Number of commits on the current branch not yet in origin/{base_branch}."""
    proc = _run(
        ["git", "rev-list", "--count", f"origin/{base_branch}...HEAD"],
        cwd=workspace,
        check=False,
    )
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def has_changes(workspace: Path) -> bool:
    proc = _run(
        ["git", "status", "--porcelain", "--", ".", ":(exclude).agents"],
        cwd=workspace,
        check=False,
    )
    return bool(proc.stdout.strip())


def commit_all(workspace: Path, message: str) -> None:
    """Stage all changes except .agents/ artifacts and commit.

    Prefers the pathspec exclusion, but falls back to a plain `git add -A` when
    the repository already ignores `.agents` (git refuses the exclude pathspec
    when the excluded path is also ignored).
    """
    proc = _run(
        ["git", "add", "-A", "--", ".", ":(exclude).agents"],
        cwd=workspace,
        check=False,
    )
    if proc.returncode != 0:
        _run(["git", "add", "-A", "--", "."], cwd=workspace)
    proc = _run(["git", "commit", "-m", message], cwd=workspace, check=False,
                env=_github_env_for_remote(workspace, "origin"))
    if proc.returncode != 0:
        raise NoChangesError(f"nothing to commit: {proc.stderr.strip()}")


def push_branch(
    workspace: Path,
    branch: str,
    remote: str = "origin",
    *,
    allow_force_with_lease: bool | None = None,
) -> None:
    """Push a branch, optionally retrying with ``--force-with-lease``.

    Force-with-lease is only safe for orchestrator-owned issue branches.  PR
    publication passes ``False`` explicitly so contributor history conflicts
    are surfaced instead of overwriting a branch we do not own.  ``None``
    retains compatibility for direct callers and enables it only for the
    historical ``ai/issue-*`` namespace.
    """
    if allow_force_with_lease is None:
        allow_force_with_lease = branch.startswith("ai/issue-")
    environment = (_github_env_for_url(remote) if "://" in remote
                   else _github_env_for_remote(workspace, remote))
    proc = _run(["git", "push", "-u", remote, branch], cwd=workspace, check=False,
                env=environment)
    if proc.returncode != 0 and allow_force_with_lease:
        proc = _run(
            ["git", "push", "--force-with-lease", "-u", remote, branch],
            cwd=workspace,
            check=False, env=environment,
        )
    if proc.returncode != 0:
        raise GitError(f"git push failed in {workspace}: {proc.stderr.strip()}")


def _github_env_for_remote(cwd: Path, remote: str) -> dict[str, str] | None:
    proc = _run(["git", "remote", "get-url", remote], cwd=cwd, check=False)
    return _github_env_for_url(proc.stdout.strip())


def _github_env_for_url(url: str) -> dict[str, str] | None:
    if "github.com" not in url:
        return None
    identity = _identity.get()
    return identity.git_environment() if identity else github_auth.git_environment()


def diff_stat(workspace: Path, base_branch: str) -> str:
    proc = _run(
        ["git", "diff", "--stat", f"origin/{base_branch}...HEAD"],
        cwd=workspace,
        check=False,
    )
    return proc.stdout.strip()
