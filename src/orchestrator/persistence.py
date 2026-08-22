"""SQLite persistence: tasks table + LangGraph checkpointer (PLAN.md section 20)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from orchestrator import config, state as state_mod


class PersistenceError(Exception):
    pass


class TaskStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (config.STATE_DIR / "orchestrator.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # LangGraph runs nodes in a thread pool, so the checkpointer gets its own
        # connection: sharing one connection between worker threads and the main
        # thread corrupts sqlite3's transaction tracking under lock contention.
        self.checkpoint_conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._ensure_tasks_schema()
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS handled_comments (
                comment_id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                handled_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._safe_commit()

    @staticmethod
    def _create_tasks_sql(table: str = "tasks") -> str:
        return f"""
            CREATE TABLE {table} (
                task_id TEXT PRIMARY KEY NOT NULL,
                repository TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace TEXT,
                branch TEXT,
                input_provider TEXT,
                output_provider TEXT,
                external_id TEXT,
                publication_url TEXT,
                error TEXT,
                current_node TEXT,
                node_started_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """

    def _ensure_tasks_schema(self) -> None:
        """Create the current task schema; old databases must be recreated."""
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        desired = {
            "task_id", "repository", "status", "workspace", "branch", "input_provider",
            "output_provider", "external_id", "publication_url", "error", "current_node",
            "node_started_at", "created_at", "updated_at",
        }
        if not exists:
            self.conn.execute(self._create_tasks_sql())
            return
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        if columns != desired:
            raise PersistenceError(
                "existing database uses an unsupported schema; remove it and start fresh"
            )

    def _safe_commit(self) -> None:
        """Commit, converting transaction conflicts into a clear PersistenceError."""
        try:
            self.conn.commit()
        except sqlite3.OperationalError as exc:
            self.conn.rollback()
            raise PersistenceError(f"sqlite commit failed: {exc}") from exc

    def checkpointer(self) -> SqliteSaver:
        return SqliteSaver(self.checkpoint_conn)

    def create_task(
        self,
        task_id: str,
        repository: str,
        status: str = state_mod.RECEIVED,
    ) -> None:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(status, str):
            status = state_mod.RECEIVED
        self.conn.execute(
            """
            INSERT OR IGNORE INTO tasks (task_id, repository, status)
            VALUES (?, ?, ?)
            """,
            (task_id.strip(), repository, status),
        )
        self._safe_commit()

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        workspace: str | None = None,
        branch: str | None = None,
        external_id: str | None = None,
        error: str | None = None,
        input_provider: str | None = None,
        output_provider: str | None = None,
        publication_url: str | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[object] = []
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if workspace is not None:
            fields.append("workspace = ?")
            values.append(workspace)
        if branch is not None:
            fields.append("branch = ?")
            values.append(branch)
        if external_id is not None:
            fields.append("external_id = ?")
            values.append(external_id)
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if input_provider is not None:
            fields.append("input_provider = ?")
            values.append(input_provider)
        if output_provider is not None:
            fields.append("output_provider = ?")
            values.append(output_provider)
        if publication_url is not None:
            fields.append("publication_url = ?")
            values.append(publication_url)
        if not fields:
            return
        fields.append("updated_at = datetime('now')")
        values.append(task_id)
        self.conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?",
            values,
        )
        self._safe_commit()

    def touch(self, task_id: str, node: str | None = None) -> None:
        """Bump updated_at (heartbeat) and track the node currently running."""
        if node is not None:
            self.conn.execute(
                """
                UPDATE tasks SET updated_at = datetime('now'),
                                 current_node = ?, node_started_at = datetime('now')
                WHERE task_id = ?
                """,
                (node, task_id),
            )
        else:
            self.conn.execute(
                "UPDATE tasks SET updated_at = datetime('now') WHERE task_id = ?",
                (task_id,),
            )
        self._safe_commit()

    def clear_error(self, task_id: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET error = NULL, updated_at = datetime('now') WHERE task_id = ?",
            (task_id,),
        )
        self._safe_commit()

    def clear_node(self, task_id: str) -> None:
        """Clear node tracking when a task reaches a terminal state."""
        self.conn.execute(
            "UPDATE tasks SET current_node = NULL, node_started_at = NULL WHERE task_id = ?",
            (task_id,),
        )
        self._safe_commit()

    def get_task(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        return result

    def list_tasks(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        results = []
        for row in rows:
            value = dict(row)
            results.append(value)
        return results

    def exists_task(self, task_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return row is not None

    # ------------------------------------------------------------ comments

    def is_comment_handled(self, comment_id: int, stale_after_seconds: int = 0) -> bool:
        """True if the comment was handled and its handling is not stale.

        Comments left at STARTED (crashed in-flight runs) older than
        `stale_after_seconds` are treated as unhandled so they can re-trigger.
        """
        row = self.conn.execute(
            "SELECT status, handled_at FROM handled_comments WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()
        if row is None:
            return False
        if row["status"] == "STARTED" and stale_after_seconds > 0:
            handled_at = datetime.strptime(row["handled_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            if datetime.now(timezone.utc) - handled_at > timedelta(seconds=stale_after_seconds):
                return False
        return True

    def mark_comment_handled(
        self,
        comment_id: int,
        task_id: str,
        repository: str,
        issue_number: int,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO handled_comments
                (comment_id, task_id, repository, issue_number, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (comment_id, task_id, repository, issue_number, status),
        )
        self._safe_commit()

    def update_comment_status(self, comment_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE handled_comments SET status = ? WHERE comment_id = ?",
            (status, comment_id),
        )
        self._safe_commit()

    def close(self) -> None:
        self.conn.close()
        self.checkpoint_conn.close()
