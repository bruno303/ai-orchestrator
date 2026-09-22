from datetime import datetime, timezone

from orchestrator.infra.filesystem import workspace


def test_expire_task_logs_uses_latest_file_and_is_idempotent(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    expired, recent = logs / "expired", logs / "recent"
    expired.mkdir(parents=True)
    recent.mkdir()
    old_file, recent_file = expired / "events.jsonl", recent / "plan.log"
    old_file.write_text("old")
    recent_file.write_text("new")
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    old = now.timestamp() - 8 * 86400
    fresh = now.timestamp() - 1 * 86400
    import os
    os.utime(old_file, (old, old))
    os.utime(recent_file, (fresh, fresh))
    monkeypatch.setattr(workspace, "LOGS_DIR", logs)

    workspace.expire_task_logs(now=now)
    workspace.expire_task_logs(now=now)

    assert not expired.exists()
    assert recent.exists()
