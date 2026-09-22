"""Tests for the retention sweep (expired workspaces, logs, stale worktrees)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from orchestrator.infra.filesystem import workspace as fs
from orchestrator.infra.git import client as git
from orchestrator.main import cli, config, maintenance
from orchestrator.main.maintenance import sweep_once


def _age(path: Path, days: float, *, follow: bool = True) -> None:
    """Set a path's mtime in the past (non-recursive unless follow)."""
    timestamp = time.time() - days * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp), follow_symlinks=follow)


def _age_tree(root: Path, days: float) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            _age(path, days)
        except OSError:
            # Symlinked or already-removed entries are fine to skip.
            pass
    _age(root, days)


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True)


def test_sweep_removes_expired_workspace_and_logs(tmp_path, monkeypatch, capsys):
    workspaces = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    repos = tmp_path / "repos"
    for directory in (workspaces, logs, repos):
        directory.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    monkeypatch.setattr(git, "REPOS_DIR", repos)

    expired = workspaces / "owner-repo-old"
    expired.mkdir()
    (expired / "work.txt").write_text("leftover\n")
    expired_logs = logs / "owner-repo-old"
    expired_logs.mkdir()
    (expired_logs / "events.jsonl").write_text("{}\n")

    fresh = workspaces / "owner-repo-new"
    fresh.mkdir()
    (fresh / "work.txt").write_text("active\n")
    fresh_logs = logs / "owner-repo-new"
    fresh_logs.mkdir()
    (fresh_logs / "events.jsonl").write_text("{}\n")

    _age_tree(expired, days=10)
    _age_tree(expired_logs, days=10)
    _age_tree(fresh, days=1)
    _age_tree(fresh_logs, days=1)

    report = sweep_once(7)

    assert report.enabled
    assert "owner-repo-old" in report.workspaces_removed
    assert "owner-repo-old" in report.logs_removed
    assert not expired.exists()
    assert not expired_logs.exists()
    assert fresh.exists()
    assert fresh_logs.exists()
    assert report.errors == []
    assert "removed 1 workspaces, 1 log dirs" in capsys.readouterr().out


def test_sweep_keeps_workspace_when_only_logs_are_fresh(tmp_path, monkeypatch):
    """An active event log must protect an old-looking workspace from the sweep."""
    workspaces = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    workspaces.mkdir()
    logs.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    monkeypatch.setattr(git, "REPOS_DIR", tmp_path / "repos")
    (tmp_path / "repos").mkdir()

    workspace = workspaces / "owner-repo-active"
    workspace.mkdir()
    _age_tree(workspace, days=30)
    active_logs = logs / "owner-repo-active"
    active_logs.mkdir()
    (active_logs / "events.jsonl").write_text("{}\n")
    _age(active_logs, days=0.1)
    _age(active_logs / "events.jsonl", days=0.1)

    report = sweep_once(7)

    assert workspace.exists()
    assert report.workspaces_removed == []


def test_sweep_never_touches_state_or_base_repos(tmp_path, monkeypatch):
    workspaces = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    repos = tmp_path / "repos"
    workspaces.mkdir()
    logs.mkdir()
    repos.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    monkeypatch.setattr(git, "REPOS_DIR", repos)

    # A real base clone with a commit so worktree commands can run against it.
    repo_dir = repos / "owner-repo"
    _run("git", "init", str(repo_dir))
    (repo_dir / "seed.txt").write_text("seed\n")
    _run("git", "add", ".", cwd=repo_dir)
    _run("git", "commit", "-m", "init", cwd=repo_dir)
    _age_tree(repo_dir, days=30)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "orchestrator.db").write_text("db")
    _age(state_dir, days=30)

    old_workspace = workspaces / "owner-repo-gone"
    old_workspace.mkdir()
    _age_tree(old_workspace, days=30)

    report = sweep_once(7)

    assert repo_dir.exists()
    assert (repo_dir / "seed.txt").exists()
    assert state_dir.exists()
    assert (state_dir / "orchestrator.db").exists()
    assert not old_workspace.exists()
    assert report.errors == []


def test_sweep_removes_expired_registered_worktree_and_prunes_registration(tmp_path, monkeypatch):
    workspaces = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    repos = tmp_path / "repos"
    workspaces.mkdir()
    logs.mkdir()
    repos.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    monkeypatch.setattr(git, "REPOS_DIR", repos)

    repo_dir = repos / "owner-repo"
    _run("git", "init", str(repo_dir))
    (repo_dir / "seed.txt").write_text("seed\n")
    _run("git", "add", ".", cwd=repo_dir)
    _run("git", "commit", "-m", "init", cwd=repo_dir)

    task_ws = workspaces / "owner-repo-1"
    _run("git", "worktree", "add", "--detach", str(task_ws), "HEAD", cwd=repo_dir)
    _age_tree(task_ws, days=30)

    # A second registration whose directory is already gone (crash residue).
    stale = workspaces / "owner-repo-stale"
    _run("git", "worktree", "add", "--detach", str(stale), "HEAD", cwd=repo_dir)
    _age(stale, days=30)
    shutil.rmtree(stale)

    report = sweep_once(7)

    assert not task_ws.exists()
    registered = git.list_worktrees(repo_dir)
    assert task_ws not in registered
    assert stale not in registered
    assert report.errors == []


def test_sweep_removes_empty_parents_without_touching_root(tmp_path, monkeypatch):
    workspaces = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    container = workspaces / "discussion-"
    container.mkdir(parents=True)
    logs.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    monkeypatch.setattr(git, "REPOS_DIR", tmp_path / "repos")
    (tmp_path / "repos").mkdir()

    _age_tree(container, days=30)

    report = sweep_once(7)

    assert not container.exists()
    assert workspaces.exists()  # root is never removed
    assert report.errors == []


def test_sweep_disabled_when_retention_below_one_day(tmp_path, monkeypatch, capsys):
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(git, "REPOS_DIR", tmp_path / "repos")
    (tmp_path / "repos").mkdir()

    old = workspaces / "owner-repo-old"
    old.mkdir()
    _age_tree(old, days=30)

    report = sweep_once(0)

    assert not report.enabled
    assert old.exists()
    assert "sweep: disabled" in capsys.readouterr().out


def test_sweep_does_not_follow_symlinks_outside_roots(tmp_path, monkeypatch):
    workspaces = tmp_path / "workspaces"
    logs = tmp_path / "logs"
    repos = tmp_path / "repos"
    for directory in (workspaces, logs, repos):
        directory.mkdir()
    monkeypatch.setattr(fs, "WORKSPACES_DIR", workspaces)
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    monkeypatch.setattr(git, "REPOS_DIR", repos)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete\n")

    link = workspaces / "owner-repo-link"
    link.symlink_to(outside, target_is_directory=True)
    _age(link, days=30, follow=False)

    report = sweep_once(7)

    # The link itself is expired residue and goes away; its target is untouched.
    assert not link.is_symlink()
    assert (outside / "precious.txt").read_text() == "do not delete\n"
    assert report.errors == []


def test_purge_task_logs_rejects_path_escape(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(fs, "LOGS_DIR", logs)
    outside = tmp_path / "outside"
    outside.mkdir()

    fs.purge_task_logs("..")
    fs.purge_task_logs("../outside")
    fs.purge_task_logs("")

    assert outside.exists()


def test_retention_config_defaults_and_env_override(monkeypatch):
    assert config.RETENTION_DAYS == 7
    assert config.SWEEP_INTERVAL_SECONDS == 3600

    import importlib
    monkeypatch.setenv("ORCHESTRATOR_RETENTION_DAYS", "3")
    monkeypatch.setenv("ORCHESTRATOR_SWEEP_INTERVAL", "60")
    importlib.reload(config)
    try:
        assert config.RETENTION_DAYS == 3
        assert config.SWEEP_INTERVAL_SECONDS == 60
    finally:
        monkeypatch.delenv("ORCHESTRATOR_RETENTION_DAYS")
        monkeypatch.delenv("ORCHESTRATOR_SWEEP_INTERVAL")
        importlib.reload(config)


def test_sweep_subcommand_uses_configured_retention(monkeypatch, capsys):
    captured = {}

    def fake_sweep(retention_days, now=None):
        captured["retention_days"] = retention_days
        return maintenance.SweepReport()

    # cmd_sweep imports sweep_once lazily from the module; patch the module attr.
    monkeypatch.setattr(maintenance, "sweep_once", fake_sweep)
    cli.cmd_sweep(SimpleNamespace(retention_days=None))
    assert captured["retention_days"] == config.RETENTION_DAYS

    cli.cmd_sweep(SimpleNamespace(retention_days=2))
    assert captured["retention_days"] == 2


def test_sweep_retention_never_breaks_the_poll_loop(monkeypatch, capsys):
    def boom(retention_days, now=None):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(maintenance, "sweep_once", boom)
    monkeypatch.setattr(config, "RETENTION_DAYS", 7)

    cli._sweep_retention()  # must not raise

    assert "sweep error (continuing): disk gone" in capsys.readouterr().out


def test_sweep_retention_is_skipped_when_disabled(monkeypatch):
    def boom(retention_days, now=None):
        raise AssertionError("sweep must not run when retention < 1")

    monkeypatch.setattr(maintenance, "sweep_once", boom)
    monkeypatch.setattr(config, "RETENTION_DAYS", 0)

    cli._sweep_retention()
