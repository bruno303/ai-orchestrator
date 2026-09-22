"""Task workspace layout and per-task log files (PLAN.md sections 4 and 23)."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACES_DIR = Path(
    os.environ.get("ORCHESTRATOR_WORKSPACES_DIR", Path.home() / "agent-workspaces")
).expanduser()
LOGS_DIR = Path(os.environ.get("ORCHESTRATOR_DATA_DIR", Path.cwd() / "data")).expanduser() / "logs"
TASK_LOG_RETENTION = timedelta(days=7)


def safe_task_token(task_id: str) -> str:
    """Return a deterministic readable path component for an opaque ID."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task id must be a non-empty string")
    value = task_id.strip()
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "task"
    canonical_github = re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", value)
    if token != value and canonical_github is None:
        token = f"{token}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"
    return token[:180]


def task_name(task_id: str) -> str:
    return safe_task_token(task_id)


def task_workspace(task_id: str) -> Path:
    return WORKSPACES_DIR / task_name(task_id)


def review_workspace(task_id: str) -> Path:
    return WORKSPACES_DIR / safe_task_token(task_id)


def discussion_workspace(task_id: str) -> Path:
    """Return an isolated checkout path for one read-only discussion."""
    return WORKSPACES_DIR / "discussion-" / safe_task_token(task_id)


def task_logs_dir(task_id: str) -> Path:
    return LOGS_DIR / safe_task_token(task_id)


def task_event_log(task_id: str) -> Path:
    return task_logs_dir(task_id) / "events.jsonl"


def append_event(task_id: str, **fields: object) -> None:
    """Append one structured event (JSON line) to the task's events.jsonl."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": task_id,
        **fields,
    }
    log_path = task_event_log(task_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(json.dumps(event))
        fh.write("\n")


def read_events(task_id: str) -> list[dict]:
    """Read the task's structured events (best effort, newest last)."""
    log_path = task_event_log(task_id)
    if not log_path.exists():
        return []
    events: list[dict] = []
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def task_log_path(task_id: str, node: str) -> Path:
    return task_logs_dir(task_id) / f"{node}.log"


def write_task_log(task_id: str, node: str, content: str) -> Path:
    log_path = task_log_path(task_id, node)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(content)
        if not content.endswith("\n"):
            fh.write("\n")
    return log_path


def _event_timestamp(value: object) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _latest_task_end(log_directory: Path) -> tuple[datetime, str] | None:
    event_path = log_directory / "events.jsonl"
    if not event_path.is_file():
        return None

    latest: tuple[datetime, str] | None = None
    for line in event_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "task_end":
            continue
        timestamp = _event_timestamp(event.get("ts"))
        if timestamp is None:
            continue
        candidate = (timestamp, str(event.get("status", "")))
        if latest is None or candidate[0] > latest[0]:
            latest = candidate
    return latest


def prune_expired_task_logs(now: datetime | None = None) -> int:
    """Remove completed task logs after the configured retention period."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    cutoff = current - TASK_LOG_RETENTION
    if not LOGS_DIR.is_dir():
        return 0

    removed = 0
    for log_directory in LOGS_DIR.iterdir():
        if not log_directory.is_dir() or log_directory.is_symlink():
            continue
        latest = _latest_task_end(log_directory)
        if latest is None:
            continue
        completed_at, status = latest
        if status != "COMPLETED" or completed_at > cutoff:
            continue
        try:
            shutil.rmtree(log_directory)
        except OSError:
            continue
        if not log_directory.exists():
            removed += 1
    return removed
