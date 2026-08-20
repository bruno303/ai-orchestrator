"""Persistence compatibility and provider metadata tests."""

from __future__ import annotations

import sqlite3

from orchestrator.persistence import TaskStore


def test_existing_database_is_migrated_without_losing_tasks(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, repository TEXT NOT NULL, "
        "issue_number INTEGER NOT NULL, status TEXT NOT NULL, workspace TEXT, branch TEXT, "
        "pr_number INTEGER, error TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("INSERT INTO tasks (task_id, repository, issue_number, status) VALUES ('r#1', 'r', 1, 'RECEIVED')")
    conn.commit()
    conn.close()

    store = TaskStore(db)
    store.update_task("r#1", input_provider="legacy", output_provider="github")
    row = store.get_task("r#1")
    assert row["task_id"] == "r#1"
    assert row["input_provider"] == "legacy"
    assert row["output_provider"] == "github"
    assert "external_id" in row


def test_publication_url_is_persisted(tmp_path):
    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("r#2", "r", 2)
    store.update_task("r#2", publication_url="https://example.test/run/2")

    assert store.get_task("r#2")["publication_url"] == "https://example.test/run/2"


def test_generic_publication_reference_is_persisted(tmp_path):
    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("external-work-2", "project")
    store.update_task("external-work-2", external_id="change-42")

    assert store.get_task("external-work-2")["external_id"] == "change-42"


def test_checkpoint_store_uses_same_database(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite")
    assert store.checkpointer() is not None
