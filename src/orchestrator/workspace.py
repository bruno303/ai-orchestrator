"""Task workspace layout and per-task log files (PLAN.md sections 4 and 23)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import config


def task_name(repository: str, issue_number: int) -> str:
    """company/backend#123 -> company-backend-123"""
    return f"{repository.replace('/', '-')}-{issue_number}"


def task_workspace(repository: str, issue_number: int) -> Path:
    return config.WORKSPACES_DIR / task_name(repository, issue_number)


def review_workspace(repository: str, number: int) -> Path:
    return config.WORKSPACES_DIR / f"{repository.replace('/', '-')}-review-{number}"


def legacy_task_checkout(task_id: str, repository: str) -> tuple[str, Path] | None:
    """Resolve old task IDs for checkpoints created before explicit checkout data."""
    reference = task_id.rsplit("#", 1)[-1]
    if not reference.isdigit():
        return None
    return f"ai/issue-{reference}", task_workspace(repository, int(reference))


def task_logs_dir(task_id: str) -> Path:
    return config.LOGS_DIR / task_id.replace("/", "-").replace("#", "-")


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
