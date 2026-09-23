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
LOG_RETENTION_DAYS = int(os.environ.get("ORCHESTRATOR_LOG_RETENTION_DAYS", "7"))


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


def prune_empty_workspace_parents(path: Path | str, *, root: Path | None = None) -> None:
    """Remove now-empty parents of a cleaned workspace up to the workspace root.

    Workspaces may live below an intermediate directory (for example the shared
    ``discussion-`` folder). Once the workspace itself is gone, those parents are
    empty and would otherwise stay behind as residual folders.
    """
    workspace_root = (root or WORKSPACES_DIR).resolve()
    current = Path(path).resolve().parent
    while current != workspace_root and current.is_relative_to(workspace_root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def expire_task_logs(*, now: datetime | None = None, retention_days: int = LOG_RETENTION_DAYS) -> None:
    """Remove task log directories whose newest file is older than retention."""
    cutoff = (now or datetime.now(timezone.utc)).timestamp() - timedelta(days=retention_days).total_seconds()
    if not LOGS_DIR.exists():
        return
    for directory in LOGS_DIR.iterdir():
        if not directory.is_dir():
            continue
        try:
            files = [path for path in directory.rglob("*") if path.is_file()]
            if files and max(path.stat().st_mtime for path in files) < cutoff:
                shutil.rmtree(directory)
            elif not files and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory)
        except OSError as exc:
            print(f"log retention: could not remove {directory}: {exc}", flush=True)
