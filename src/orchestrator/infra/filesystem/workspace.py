"""Task workspace layout and per-task log files (PLAN.md sections 4 and 23)."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

WORKSPACES_DIR = Path(
    os.environ.get("ORCHESTRATOR_WORKSPACES_DIR", Path.home() / "agent-workspaces")
).expanduser()
LOGS_DIR = Path(os.environ.get("ORCHESTRATOR_DATA_DIR", Path.cwd() / "data")).expanduser() / "logs"


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


def remove_empty_parents(path: Path, root: Path) -> None:
    """Remove empty directories from ``path`` up to, but excluding, ``root``."""
    path = path.resolve(strict=False)
    root = root.resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError:
        return

    current = path if path.exists() else path.parent
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def task_logs_dir(task_id: str) -> Path:
    return LOGS_DIR / safe_task_token(task_id)


def _tree_mtime(path: Path) -> float:
    """Newest mtime in a directory tree without following symlinks (0.0 if absent)."""
    newest = 0.0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            stat_result = current.lstat()
        except OSError:
            continue
        newest = max(newest, stat_result.st_mtime)
        if current.is_dir() and not current.is_symlink():
            try:
                stack.extend(current.iterdir())
            except OSError:
                pass
    return newest


def task_activity(task_token: str, workspace_path: Path | None = None) -> float:
    """Return the last activity timestamp for a task token (0.0 when unknown).

    Activity is the newest mtime anywhere in the task's workspace subtree, its
    ``events.jsonl``, and its log directory contents, so files edited by an
    active run keep it alive even when no new log files are created. Nested
    layouts (e.g. ``WORKSPACES_DIR/discussion-/<token>``) are covered by the
    recursive workspace walk.
    """
    candidates: list[float] = []
    if workspace_path is not None:
        candidates.append(_tree_mtime(workspace_path))
    logs_dir = LOGS_DIR / task_token
    for path in (logs_dir, logs_dir / "events.jsonl"):
        try:
            candidates.append(path.stat().st_mtime)
        except OSError:
            continue
        if path.is_dir():
            try:
                candidates.extend(child.stat().st_mtime for child in path.iterdir())
            except OSError:
                pass
    return max(candidates, default=0.0)


def purge_task_logs(task_token: str) -> None:
    """Delete a task's log directory.

    ``task_token`` is used as an exact child name of ``LOGS_DIR`` (not
    re-tokenized), so legacy directory names such as ``review:owner-repo-15``
    remain sweepable. The guard rejects anything that could escape the root.
    """
    if not task_token or task_token in {".", ".."} or os.sep in task_token:
        return
    logs_dir = LOGS_DIR / task_token
    if logs_dir.parent != LOGS_DIR:
        return
    if logs_dir.is_symlink():
        logs_dir.unlink()
        return
    if logs_dir.is_dir():
        shutil.rmtree(logs_dir)
    remove_empty_parents(logs_dir, LOGS_DIR)


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
