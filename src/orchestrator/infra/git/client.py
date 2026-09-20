"""Git operations: cached clones, isolated workspaces, branches, commits, pushes."""

from __future__ import annotations

import os
import subprocess
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

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
    """Keep one fetched clone as a local object cache for task workspaces."""
    repo_dir = base_repo_dir(repository)
    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["git", "clone", clone_url, str(repo_dir)],
            cwd=repo_dir.parent,
            env=_github_env_for_url(clone_url),
        )
    fetch(repo_dir)
    return repo_dir


def fetch(repo_dir: Path) -> None:
    _run(
        ["git", "fetch", "origin", "--prune"],
        cwd=repo_dir,
        env=_github_env_for_remote(repo_dir, "origin"),
    )


def fetch_commit(repo_dir: Path, commit: str, remote: str = "origin") -> None:
    """Fetch an immutable commit, including one advertised by a fork remote."""
    environment = (
        _github_env_for_url(remote)
        if "github.com" in remote
        else _github_env_for_remote(repo_dir, remote)
    )
    _run(["git", "fetch", remote, commit], cwd=repo_dir, env=environment)


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


def clone_workspace(repo_dir: Path, workspace: Path, repository_url: str) -> None:
    """Create a self-contained task clone using the cached repository locally.

    A normal local clone can reuse object files efficiently without an
    ``objects/info/alternates`` dependency. The workspace's origin is then
    restored to the real repository and fetched so its remote-tracking refs are
    current. The resulting ``.git`` directory lives entirely inside workspace.
    """
    if workspace.exists() or workspace.is_symlink():
        raise GitError(f"workspace already exists: {workspace}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["git", "clone", "--no-checkout", str(repo_dir), str(workspace)],
        cwd=workspace.parent,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"workspace clone failed: {proc.stderr.strip()}")
    _run(["git", "remote", "set-url", "origin", repository_url], cwd=workspace)
    fetch(workspace)


def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    proc = _run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=repo_dir,
        check=False,
    )
    return proc.returncode == 0


def checkout_branch(
    workspace: Path,
    branch: str,
    base_branch: str,
    start_point: str | None = None,
) -> None:
    """Checkout a task branch inside an independent workspace clone.

    Existing remote branches are reused for comment-triggered follow-ups. A
    supplied immutable start point is attached directly to the requested local
    branch, which is used for PR-head execution.
    """
    if start_point:
        proc = _run(
            ["git", "checkout", "-B", branch, start_point],
            cwd=workspace,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"branch checkout failed: {proc.stderr.strip()}")
        return

    if remote_branch_exists(workspace, branch):
        proc = _run(
            ["git", "checkout", "-B", branch, f"origin/{branch}"],
            cwd=workspace,
            check=False,
        )
        if proc.returncode == 0:
            return

    proc = _run(
        ["git", "checkout", "-B", branch, f"origin/{base_branch}"],
        cwd=workspace,
        check=False,
    )
    if proc.returncode != 0:
        proc = _run(
            ["git", "checkout", "-B", branch, base_branch],
            cwd=workspace,
            check=False,
        )
    if proc.returncode != 0:
        raise GitError(f"branch checkout failed: {proc.stderr.strip()}")


def checkout_detached(workspace: Path, commit: str) -> None:
    """Checkout an immutable revision without attaching it to a local branch."""
    _run(["git", "checkout", "--detach", commit], cwd=workspace)


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
    proc = _run(
        ["git", "commit", "-m", message],
        cwd=workspace,
        check=False,
        env=_github_env_for_remote(workspace, "origin"),
    )
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

    Force-with-lease is only safe for orchestrator-owned issue branches. PR
    publication passes ``False`` explicitly so contributor history conflicts
    are surfaced instead of overwriting a branch we do not own. ``None`` retains
    compatibility for direct callers and enables it only for the historical
    ``ai/issue-*`` namespace.
    """
    if allow_force_with_lease is None:
        allow_force_with_lease = branch.startswith("ai/issue-")
    environment = (
        _github_env_for_url(remote)
        if "://" in remote
        else _github_env_for_remote(workspace, remote)
    )
    proc = _run(
        ["git", "push", "-u", remote, branch],
        cwd=workspace,
        check=False,
        env=environment,
    )
    if proc.returncode != 0 and allow_force_with_lease:
        proc = _run(
            ["git", "push", "--force-with-lease", "-u", remote, branch],
            cwd=workspace,
            check=False,
            env=environment,
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
