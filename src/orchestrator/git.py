"""Git operations: base clones, worktrees, branches, commits, pushes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator import config


class GitError(Exception):
    pass


class NoChangesError(GitError):
    pass


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
    return config.REPOS_DIR / repository.replace("/", "-")


def ensure_base_clone(repository: str, clone_url: str) -> Path:
    """Clone the repository once into REPOS_DIR if not present, then fetch."""
    repo_dir = base_repo_dir(repository)
    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", clone_url, str(repo_dir)], cwd=repo_dir.parent)
    fetch(repo_dir)
    return repo_dir


def fetch(repo_dir: Path) -> None:
    _run(["git", "fetch", "origin", "--prune"], cwd=repo_dir)


def fetch_commit(repo_dir: Path, commit: str, remote: str = "origin") -> None:
    """Fetch an immutable commit, including one advertised by a fork remote."""
    _run(["git", "fetch", remote, commit], cwd=repo_dir)


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


def remove_worktree(repo_dir: Path, workspace: Path, branch: str) -> None:
    """Remove a task worktree and its branch (used for clean re-runs)."""
    if workspace.exists():
        _run(["git", "worktree", "remove", "--force", str(workspace)], cwd=repo_dir, check=False)
    proc = _run(["git", "branch", "--list", branch], cwd=repo_dir, check=False)
    if branch in proc.stdout.split():
        _run(["git", "branch", "-D", branch], cwd=repo_dir, check=False)


def create_worktree(repo_dir: Path, workspace: Path, branch: str, base_branch: str) -> None:
    """Create an isolated worktree at `workspace` on a new `branch` from base_branch."""
    if workspace.exists():
        raise GitError(f"workspace already exists: {workspace}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
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
    proc = _run(["git", "commit", "-m", message], cwd=workspace, check=False)
    if proc.returncode != 0:
        raise NoChangesError(f"nothing to commit: {proc.stderr.strip()}")


def push_branch(workspace: Path, branch: str) -> None:
    """Push the branch; retry with --force-with-lease on non-fast-forward.

    The ai/issue-* branch is orchestrator-owned, so a stale remote copy from a
    previous run of the same issue is safely overwritten.
    """
    proc = _run(["git", "push", "-u", "origin", branch], cwd=workspace, check=False)
    if proc.returncode != 0:
        proc = _run(
            ["git", "push", "--force-with-lease", "-u", "origin", branch],
            cwd=workspace,
            check=False,
        )
    if proc.returncode != 0:
        raise GitError(f"git push failed in {workspace}: {proc.stderr.strip()}")


def diff_stat(workspace: Path, base_branch: str) -> str:
    proc = _run(
        ["git", "diff", "--stat", f"origin/{base_branch}...HEAD"],
        cwd=workspace,
        check=False,
    )
    return proc.stdout.strip()
