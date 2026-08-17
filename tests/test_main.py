"""Light tests for the CLI layer."""

from __future__ import annotations

import pytest

from orchestrator import github
from orchestrator.main import _parse_ref, cmd_poll


class FakeGraph:
    """Minimal graph double exposing stream() + get_state() (LangGraph API used by main)."""

    def __init__(self, started: list[str], final_status: str = "COMPLETED"):
        self.started = started
        self.final_status = final_status
        self.streamed: list[tuple[str, dict]] = []

    def stream(self, seed, config=None, stream_mode=None):
        task_id = (seed or {}).get("task_id")
        if task_id is not None:
            self.started.append(task_id)
        yield {"create_pr": {"status": self.final_status, "task_id": task_id, "pr_number": 42}}

    def get_state(self, config):
        values = {"status": self.final_status, "task_id": config["configurable"]["thread_id"], "pr_number": 42}
        return type("State", (), {"values": values})()


def test_parse_ref():
    assert _parse_ref("company/backend#123") == ("company/backend", 123)


def test_parse_ref_invalid():
    with pytest.raises(SystemExit):
        _parse_ref("not-a-ref")


def test_poll_once_skips_existing(allowlist, tmp_path, monkeypatch, capsys):
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#1", "company/backend", 1)

    issue = github.Issue(number=1, title="t", body="b", html_url="u")
    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [])
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)

    cmd_poll(type("A", (), {"once": True})())
    assert "new issue" not in capsys.readouterr().out


def test_poll_once_new_issue(allowlist, tmp_path, monkeypatch, capsys):
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    issue = github.Issue(number=9, title="t", body="b", html_url="u")
    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [])
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.github.get_issue", lambda repo, n: issue)

    started: list[str] = []
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph(started))

    cmd_poll(type("A", (), {"once": True})())
    out = capsys.readouterr().out
    assert "new issue: company/backend#9" in out
    assert "[create_pr] COMPLETED" in out
    assert started == ["company/backend#9"]
    task = store.get_task("company/backend#9")
    assert task["status"] == "COMPLETED"
    assert task["pr_number"] == 42


def test_run_force_resets_state(allowlist, tmp_path, monkeypatch):
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#7", "company/backend", 7)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, checkpoint TEXT, metadata TEXT)"
    )
    store.conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) VALUES (?, ?, ?, ?, ?)",
        ("company/backend#7", "", "1", "{}", "{}"),
    )
    store.conn.commit()
    store.update_task("company/backend#7", status="FAILED", error="old error")

    issue = github.Issue(number=7, title="t", body="b", html_url="u")
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph([]))

    main.main(["run", "company/backend#7", "--force"])

    row = store.conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'company/backend#7'").fetchone()[0]
    assert row == 0
    task = store.get_task("company/backend#7")
    assert task["error"] is None
    assert task["status"] == "COMPLETED"


def test_resume_drops_failed_checkpoint(allowlist, tmp_path, monkeypatch, capsys):
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#6", "company/backend", 6)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, checkpoint TEXT, metadata TEXT)"
    )
    store.conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) VALUES (?, ?, ?, ?, ?)",
        ("company/backend#6", "", "1", "{}", "{}"),
    )
    store.conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) VALUES (?, ?, ?, ?, ?)",
        ("company/backend#6", "", "2", '{"status": "FAILED", "task_id": "company/backend#6"}', "{}"),
    )
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value TEXT)"
    )
    store.conn.execute(
        "INSERT INTO writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("company/backend#6", "", "2", "t", 0, "status", "update", '"FAILED"'),
    )
    store.conn.commit()

    class StatefulGraph(FakeGraph):
        def get_state(self, config):
            values = {"status": "FAILED", "task_id": "company/backend#6"}
            return type("State", (), {"values": values})()

    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: StatefulGraph([]))

    main.main(["resume", "company/backend#6"])
    out = capsys.readouterr().out
    assert "[create_pr] COMPLETED" in out
    remaining = [r[2] for r in store.conn.execute("SELECT * FROM checkpoints WHERE thread_id = 'company/backend#6'")]
    assert remaining == ["1"]
    writes = store.conn.execute("SELECT COUNT(*) FROM writes WHERE thread_id = 'company/backend#6'").fetchone()[0]
    assert writes == 0


def test_reset_deletes_task(allowlist, tmp_path, monkeypatch, capsys):
    """reset removes the tasks row + checkpoints and does not run the graph."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#7", "company/backend", 7)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, checkpoint TEXT, metadata TEXT)"
    )
    store.conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) VALUES (?, ?, ?, ?, ?)",
        ("company/backend#7", "", "1", "{}", "{}"),
    )
    store.conn.commit()
    store.update_task("company/backend#7", status="FAILED", error="old error")

    started: list[str] = []
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph(started))

    main.main(["reset", "company/backend#7"])

    assert store.get_task("company/backend#7") is None
    row = store.conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'company/backend#7'").fetchone()[0]
    assert row == 0
    assert started == []
    assert "deleted; will re-run on next poll" in capsys.readouterr().out


def test_poll_reruns_deleted_task(allowlist, tmp_path, monkeypatch, capsys):
    """After reset (task row deleted), the next poll re-runs the issue."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#9", "company/backend", 9)
    store.conn.execute("DELETE FROM tasks WHERE task_id = 'company/backend#9'")
    store.conn.commit()

    issue = github.Issue(number=9, title="t", body="b", html_url="u")
    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [])
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.github.get_issue", lambda repo, n: issue)

    started: list[str] = []
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph(started))

    cmd_poll(type("A", (), {"once": True})())
    out = capsys.readouterr().out
    assert "new issue: company/backend#9" in out
    assert started == ["company/backend#9"]


def test_reset_unknown_task_hint(allowlist, tmp_path, monkeypatch, capsys):
    """reset on a nonexistent id prints the list hint and exits non-zero."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)

    with pytest.raises(SystemExit) as exc:
        main.main(["reset", "company/backend#404"])
    assert "orchestrator list" in str(exc.value)


def test_list_shows_ids(allowlist, tmp_path, monkeypatch, capsys):
    """cmd_list prints the task id in owner/repo#issue format clearly."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#7", "company/backend", 7)
    store.update_task("company/backend#7", status="COMPLETED", pr_number=42)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)

    main.cmd_list(type("A", (), {}))
    out = capsys.readouterr().out
    assert "company/backend#7" in out
    assert "id (owner/repo#issue)" in out


def test_poll_once_label_filter(allowlist, tmp_path, monkeypatch, capsys):
    """Repos with a configured label only get issues carrying that label."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    allowlist.write_text("repositories:\n  - name: company/backend\n    label: ai-agent\n")
    main.config.load_repository_config.cache_clear()

    store = TaskStore(tmp_path / "db.sqlite")
    labeled = github.Issue(number=1, title="t", body="b", html_url="u", labels=["ai-agent"])
    unlabeled = github.Issue(number=2, title="t2", body="b", html_url="u")
    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [labeled, unlabeled])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [])
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph([]))

    cmd_poll(type("A", (), {"once": True})())
    out = capsys.readouterr().out
    assert "new issue: company/backend#1" in out
    assert "company/backend#2" not in out


def test_poll_comment_trigger(allowlist, tmp_path, monkeypatch, capsys):
    """A /ai-agent comment triggers a full re-run with eyes/rocket reactions."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    allowlist.write_text("repositories:\n  - name: company/backend\n")
    main.config.load_repository_config.cache_clear()

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#1", "company/backend", 1)
    store.update_task("company/backend#1", status="COMPLETED", pr_number=12)

    issue = github.Issue(number=1, title="t", body="b", html_url="u")
    comment = github.IssueComment(id=555, body="/ai-agent please change the color", user_login="bruno")
    reactions: list[tuple[str, int, str]] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue)
    monkeypatch.setattr(
        github, "add_reaction", lambda repo, comment_id, content: reactions.append((repo, comment_id, content))
    )
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph([]))

    cmd_poll(type("A", (), {"once": True})())
    out = capsys.readouterr().out
    assert "command comment 555" in out
    assert reactions == [("company/backend", 555, "eyes"), ("company/backend", 555, "rocket")]
    assert store.is_comment_handled(555)
    row = store.conn.execute("SELECT status FROM handled_comments WHERE comment_id = 555").fetchone()
    assert row[0] == "COMPLETED"
    # re-run was full: task reset then completed
    task = store.get_task("company/backend#1")
    assert task["status"] == "COMPLETED"


def test_poll_comment_already_handled(allowlist, tmp_path, monkeypatch, capsys):
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#1", "company/backend", 1)
    store.update_task("company/backend#1", status="COMPLETED", pr_number=12)
    store.mark_comment_handled(555, "company/backend#1", "company/backend", 1, "COMPLETED")

    issue = github.Issue(number=1, title="t", body="b", html_url="u")
    comment = github.IssueComment(id=555, body="/ai-agent again", user_login="bruno")
    started: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment])
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph(started))

    cmd_poll(type("A", (), {"once": True})())
    assert started == []
    assert "command comment" not in capsys.readouterr().out


def test_poll_comment_on_pr(allowlist, tmp_path, monkeypatch, capsys):
    """A /ai-agent comment on an orchestrator PR maps to the linked issue task."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#3", "company/backend", 3)
    store.update_task("company/backend#3", status="COMPLETED", pr_number=20)

    issue3 = github.Issue(number=3, title="t3", body="b", html_url="u")
    comment = github.IssueComment(id=777, body="/ai-agent fix the tests", user_login="bruno")
    pr = github.PullRequest(number=20, head_ref="ai/issue-3")
    reactions: list[tuple[str, int, str]] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue3])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [pr])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment] if number == 20 else [])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue3)
    monkeypatch.setattr(
        github, "add_reaction", lambda repo, comment_id, content: reactions.append((repo, comment_id, content))
    )
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph([]))

    cmd_poll(type("A", (), {"once": True})())
    out = capsys.readouterr().out
    assert "command comment 777" in out
    assert "company/backend#3" in out
    assert reactions == [("company/backend", 777, "eyes"), ("company/backend", 777, "rocket")]
    assert store.is_comment_handled(777)


class SeedCaptureGraph(FakeGraph):
    def __init__(self, started: list[str], seeds: list[dict]):
        super().__init__(started)
        self.seeds = seeds

    def stream(self, seed, config=None, stream_mode=None):
        self.seeds.append(seed or {})
        return super().stream(seed, config=config, stream_mode=stream_mode)


def test_pr_comment_trigger_includes_pr_context(allowlist, tmp_path, monkeypatch, capsys):
    """A /ai-agent comment on a PR seeds the run with the PR context block."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#3", "company/backend", 3)
    store.update_task("company/backend#3", status="COMPLETED", pr_number=14)

    issue3 = github.Issue(number=3, title="t3", body="b3", html_url="u")
    comment = github.IssueComment(id=777, body="/ai-agent fix the tests", user_login="bruno")
    pr = github.PullRequest(number=14, head_ref="ai/issue-3")
    pr_detail = github.PullRequestDetail(
        number=14, title="feat: backlog", body="Closes #3", url="https://x/14",
        base_ref="main", head_ref="ai/issue-3",
        files=[("src/app/page.tsx", "modified"), ("src/new.ts", "added")],
    )
    seeds: list[dict] = []
    started: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue3])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [pr])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment] if number == 14 else [])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue3)
    monkeypatch.setattr(github, "get_pull_request", lambda repo, n: pr_detail)
    monkeypatch.setattr(github, "add_reaction", lambda repo, cid, content: None)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr(
        "orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: SeedCaptureGraph(started, seeds)
    )

    cmd_poll(type("A", (), {"once": True})())
    seed = seeds[0]
    assert seed["pr_number"] == 14
    context = "\n".join(seed["extra_context"])
    assert "<pr>" in context
    assert "number: 14" in context
    assert "changed files: src/app/page.tsx (modified), src/new.ts (added)" in context
    assert "fix the tests" in context


def test_issue_comment_trigger_with_open_pr(allowlist, tmp_path, monkeypatch, capsys):
    """Issue comment on a task with an open PR also gets the PR context."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#3", "company/backend", 3)
    store.update_task("company/backend#3", status="COMPLETED", pr_number=14)

    issue3 = github.Issue(number=3, title="t3", body="b3", html_url="u")
    comment = github.IssueComment(id=778, body="/ai-agent more", user_login="bruno")
    pr_detail = github.PullRequestDetail(
        number=14, title="feat: backlog", body="Closes #3", url="https://x/14",
        base_ref="main", head_ref="ai/issue-3", files=[],
    )
    seeds: list[dict] = []
    started: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue3])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment] if number == 3 else [])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue3)
    monkeypatch.setattr(github, "find_open_pr", lambda repo, branch: 14)
    monkeypatch.setattr(github, "get_pull_request", lambda repo, n: pr_detail)
    monkeypatch.setattr(github, "add_reaction", lambda repo, cid, content: None)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr(
        "orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: SeedCaptureGraph(started, seeds)
    )

    cmd_poll(type("A", (), {"once": True})())
    seed = seeds[0]
    assert seed["pr_number"] == 14
    assert "<pr>" in "\n".join(seed["extra_context"])


def test_issue_comment_trigger_without_pr(allowlist, tmp_path, monkeypatch, capsys):
    """First trigger (no PR yet): context has the comment only, no PR block."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    issue3 = github.Issue(number=3, title="t3", body="b3", html_url="u")
    comment = github.IssueComment(id=779, body="/ai-agent start", user_login="bruno")
    seeds: list[dict] = []
    started: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue3])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment] if number == 3 else [])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue3)
    monkeypatch.setattr(github, "find_open_pr", lambda repo, branch: None)
    monkeypatch.setattr(github, "add_reaction", lambda repo, cid, content: None)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr(
        "orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: SeedCaptureGraph(started, seeds)
    )

    cmd_poll(type("A", (), {"once": True})())
    seed = seeds[0]
    assert seed.get("pr_number") is None
    assert "<pr>" not in "\n".join(seed["extra_context"])


def test_pr_fetch_failure_tolerated(allowlist, tmp_path, monkeypatch, capsys):
    """If PR metadata fetch fails, the trigger still runs without the block."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#3", "company/backend", 3)
    store.update_task("company/backend#3", status="COMPLETED", pr_number=14)

    issue3 = github.Issue(number=3, title="t3", body="b3", html_url="u")
    comment = github.IssueComment(id=780, body="/ai-agent do it", user_login="bruno")
    pr = github.PullRequest(number=14, head_ref="ai/issue-3")
    seeds: list[dict] = []
    started: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue3])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [pr])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment] if number == 14 else [])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue3)
    monkeypatch.setattr(github, "get_pull_request", lambda repo, n: (_ for _ in ()).throw(github.GitHubError("boom")))
    monkeypatch.setattr(github, "add_reaction", lambda repo, cid, content: None)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr(
        "orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: SeedCaptureGraph(started, seeds)
    )

    cmd_poll(type("A", (), {"once": True})())
    seed = seeds[0]
    assert seed["pr_number"] == 14
    assert "<pr>" not in "\n".join(seed["extra_context"])


def test_cmd_logs_list_and_tail(allowlist, tmp_path, monkeypatch, capsys):
    from orchestrator import main, workspace

    task_id = "company/backend#1"
    log_path = workspace.task_log_path(task_id, "plan")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line1\nline2\nline3\n")

    main.cmd_logs(type("A", (), {"task_id": task_id, "node": None, "follow": False, "lines": 2}))
    out = capsys.readouterr().out
    assert "logs for company/backend#1" in out
    assert "plan" in out
    assert "bytes" in out

    main.cmd_logs(type("A", (), {"task_id": task_id, "node": "plan", "follow": False, "lines": 2}))
    out = capsys.readouterr().out
    assert "line2" in out
    assert "line3" in out


def test_cmd_watch_once(allowlist, tmp_path, monkeypatch, capsys):
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#1", "company/backend", 1)
    store.update_task("company/backend#1", status="IMPLEMENTING")
    store.touch("company/backend#1", node="implement")

    main._orig_TaskStore = TaskStore
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)

    main.cmd_watch(type("A", (), {"once": True}))
    out = capsys.readouterr().out
    assert "company/backend#1" in out
    assert "IMPLEMENTING" in out
    assert "implement" in out


def test_task_end_event_written(allowlist, tmp_path, monkeypatch):
    from orchestrator import main, workspace
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    main._persist_result(store, {"task_id": "company/backend#1", "status": "COMPLETED", "pr_number": 42})
    events = workspace.read_events("company/backend#1")
    assert events[-1]["event"] == "task_end"
    assert events[-1]["status"] == "COMPLETED"
    assert events[-1]["pr_number"] == 42


def test_migration_and_touch(tmp_path):
    """Existing old-schema DBs gain the new columns; touch tracks the node."""
    import sqlite3

    from orchestrator.persistence import TaskStore

    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, repository TEXT NOT NULL, "
        "issue_number INTEGER NOT NULL, status TEXT NOT NULL, workspace TEXT, branch TEXT, "
        "pr_number INTEGER, error TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, repository, issue_number, status) VALUES ('r#1', 'r', 1, 'RECEIVED')"
    )
    conn.commit()
    conn.close()

    store = TaskStore(db)
    store.touch("r#1", node="plan")
    row = store.get_task("r#1")
    assert row["current_node"] == "plan"
    assert row["node_started_at"] is not None
    store.clear_node("r#1")
    assert store.get_task("r#1")["current_node"] is None


def test_workspace_events(tmp_path, monkeypatch):
    from orchestrator import workspace

    monkeypatch.setattr(workspace.config, "LOGS_DIR", tmp_path)
    workspace.append_event("r#1", event="node_start", node="plan")
    workspace.append_event("r#1", event="node_end", node="plan", status="PLANNING", duration_s=1.5)
    events = workspace.read_events("r#1")
    assert len(events) == 2
    assert events[0]["event"] == "node_start"
    assert events[0]["node"] == "plan"
    assert events[1]["duration_s"] == 1.5


def test_poll_comment_on_foreign_pr_ignored(allowlist, tmp_path, monkeypatch, capsys):
    """PRs not on an ai/issue-* branch are ignored."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    comment = github.IssueComment(id=999, body="/ai-agent hi", user_login="bruno")
    pr = github.PullRequest(number=30, head_ref="feature/whatever")
    started: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [pr])
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph(started))

    cmd_poll(type("A", (), {"once": True})())
    assert started == []
    assert not store.is_comment_handled(999)


def test_poll_second_instance_exits(allowlist, tmp_path, monkeypatch, capsys):
    """A second poll process must refuse to start (single-instance flock)."""
    import fcntl

    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    lock = main._acquire_poll_lock()  # hold the lock as the "first" poll
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    with pytest.raises(SystemExit) as exc:
        main.cmd_poll(type("A", (), {"once": True})())
    assert "already running" in str(exc.value)
    lock.close()


def test_stale_comment_recovery(tmp_path):
    """Comments left at STARTED long ago are re-triggerable."""
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.mark_comment_handled(111, "company/backend#1", "company/backend", 1, "STARTED")
    store.conn.execute(
        "UPDATE handled_comments SET handled_at = '2020-01-01 00:00:00' WHERE comment_id = 111"
    )
    store.conn.commit()
    assert store.is_comment_handled(111, stale_after_seconds=3600) is False
    # Fresh STARTED rows are still handled.
    store.mark_comment_handled(112, "company/backend#1", "company/backend", 1, "STARTED")
    assert store.is_comment_handled(112, stale_after_seconds=3600) is True
    # Terminal statuses are always handled.
    assert store.is_comment_handled(111, stale_after_seconds=0) is True


def test_detect_stale_tasks(tmp_path, capsys):
    """Active tasks with no recent activity are marked FAILED."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#1", "company/backend", 1)
    store.update_task("company/backend#1", status="PLANNING")
    store.conn.execute("UPDATE tasks SET updated_at = '2020-01-01 00:00:00' WHERE task_id = 'company/backend#1'")
    store.conn.commit()
    store.create_task("company/backend#2", "company/backend", 2)
    store.update_task("company/backend#2", status="IMPLEMENTING")

    main._detect_stale_tasks(store)
    t1 = store.get_task("company/backend#1")
    t2 = store.get_task("company/backend#2")
    assert t1["status"] == "FAILED"
    assert "stale" in t1["error"]
    assert t2["status"] == "IMPLEMENTING"  # recent activity, untouched


def test_comment_trigger_skips_active_task(allowlist, tmp_path, monkeypatch, capsys):
    """The in-trigger re-check skips when the task started meanwhile."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    store.create_task("company/backend#1", "company/backend", 1)
    store.update_task("company/backend#1", status="IMPLEMENTING")

    issue = github.Issue(number=1, title="t", body="b", html_url="u")
    comment = github.IssueComment(id=321, body="/ai-agent do it", user_login="bruno")
    started: list[str] = []
    reactions: list[str] = []

    monkeypatch.setattr(github, "list_open_issues", lambda repo, label=None: [issue])
    monkeypatch.setattr(github, "list_open_pull_requests", lambda repo: [])
    monkeypatch.setattr(github, "list_issue_comments", lambda repo, number: [comment])
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue)
    monkeypatch.setattr(github, "add_reaction", lambda repo, cid, content: reactions.append(content))
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)
    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FakeGraph(started))

    cmd_poll(type("A", (), {"once": True})())
    assert started == []
    assert reactions == []
    assert not store.is_comment_handled(321)


def test_main_handles_keyboard_interrupt(monkeypatch, capsys):
    """Ctrl+C exits cleanly with a hint, not a traceback."""
    from orchestrator import main as main_mod

    def boom(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "cmd_poll", boom)
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["poll"])
    assert exc.value.code == 130
    out = capsys.readouterr().out
    assert "interrupted" in out
    assert "resume" in out
    assert "Traceback" not in out


def test_run_streams_progress_and_failure(allowlist, tmp_path, monkeypatch, capsys):
    """run exits non-zero-free path: FAILED node streams, task row gets error."""
    from orchestrator import main
    from orchestrator.persistence import TaskStore

    store = TaskStore(tmp_path / "db.sqlite")
    issue = github.Issue(number=5, title="t", body="b", html_url="u")
    monkeypatch.setattr(github, "get_issue", lambda repo, n: issue)
    monkeypatch.setattr("orchestrator.main.TaskStore", lambda: store)

    class FailingGraph(FakeGraph):
        def stream(self, seed, config=None, stream_mode=None):
            yield {"plan": {"status": "FAILED", "task_id": seed["task_id"], "error": "opencode exited with 1"}}

        def get_state(self, config):
            values = {"status": "FAILED", "task_id": config["configurable"]["thread_id"], "error": "opencode exited with 1"}
            return type("State", (), {"values": values})()

    monkeypatch.setattr("orchestrator.main.build_graph", lambda checkpointer, on_node_start=None: FailingGraph([]))
    main.main(["run", "company/backend#5"])
    out = capsys.readouterr().out
    assert "[plan] FAILED" in out
    assert "opencode exited with 1" in out
    task = store.get_task("company/backend#5")
    assert task["status"] == "FAILED"
    assert "opencode exited with 1" in task["error"]