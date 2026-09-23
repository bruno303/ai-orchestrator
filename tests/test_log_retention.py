"""Task log expiry keeps recent logs and removes stale ones."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from orchestrator.infra.filesystem import workspace


def _age(path: Path, now: datetime, days: int) -> None:
    timestamp = now.timestamp() - days * 86400
    import os

    os.utime(path, (timestamp, timestamp))


def test_expire_task_logs_removes_only_stale_directories(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    expired, recent = logs / "expired", logs / "recent"
    expired.mkdir(parents=True)
    recent.mkdir(parents=True)
    (expired / "events.jsonl").write_text("old")
    (expired / "plan.log").write_text("old")
    (recent / "plan.log").write_text("new")
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    _age(expired / "events.jsonl", now, 10)
    _age(expired / "plan.log", now, 8)
    _age(recent / "plan.log", now, 1)
    monkeypatch.setattr(workspace, "LOGS_DIR", logs)

    workspace.expire_task_logs(now=now)
    workspace.expire_task_logs(now=now)

    assert not expired.exists()
    assert recent.exists()


def test_expire_task_logs_keeps_a_directory_with_one_recent_file(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    task = logs / "task"
    task.mkdir(parents=True)
    (task / "old.log").write_text("old")
    (task / "new.log").write_text("new")
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    _age(task / "old.log", now, 30)
    _age(task / "new.log", now, 1)
    monkeypatch.setattr(workspace, "LOGS_DIR", logs)

    workspace.expire_task_logs(now=now)

    assert task.exists()
