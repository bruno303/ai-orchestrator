"""CLI: run / execute / review / list / status / resume."""

from __future__ import annotations

import argparse
import fcntl
import inspect
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator import config, git, github, state as state_mod, workspace
from orchestrator.application import PollingApplication, compose_runtime
from orchestrator.graph import build_graph
from orchestrator.persistence import PersistenceError, TaskStore
from orchestrator.review import compose_review_runtime

ISSUE_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def _parse_ref(ref: str) -> tuple[str, int]:
    match = ISSUE_REF_RE.match(ref)
    if not match:
        sys.exit(f"invalid issue reference {ref!r}; expected owner/repo#number")
    return match.group(1), int(match.group(2))


def _seed_state(store: TaskStore, repository: str, issue_number: int) -> dict:
    issue = github.get_issue(repository, issue_number)
    task_id = f"{repository}#{issue_number}"
    store.create_task(task_id, repository, issue_number)
    return {
        "task_id": task_id,
        "input": {"provider": "github", "data": {"repository": repository, "number": issue_number,
            "title": issue.title, "body": issue.body}, "provider_state": {}},
        "processing": {}, "workspace": {"branch": f"ai/issue-{issue_number}"}, "output": {},
        "status": state_mod.RECEIVED,
        "iteration": 1,
        "phase_attempts": 1,
    }


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _run_graph(
    store: TaskStore,
    seed: dict | None,
    task_id: str,
    *,
    executor=None,
    workspace_manager=None,
    destination=None,
) -> dict:
    """Run (or resume) the graph, streaming per-node progress to stdout and DB.

    The graph is built per run so the heartbeat callback knows the task_id.
    Status persistence is best-effort: a PersistenceError never crashes the run
    (the LangGraph checkpoint remains the source of truth).
    """
    if executor is None and workspace_manager is None and destination is None:
        runtime = compose_runtime(store)
        executor = runtime.executor
        workspace_manager = runtime.workspace_manager
        destination = runtime.destination

    node_starts: dict[str, float] = {}

    def on_node_start(node: str, state: dict) -> None:
        node_starts[node] = time.monotonic()
        try:
            store.touch(task_id, node=node)
            workspace.append_event(task_id, event="node_start", node=node)
        except PersistenceError as exc:
            print(f"[{_now()}] [{node}] warning: heartbeat failed: {exc}", flush=True)

    graph_kwargs = {"on_node_start": on_node_start}
    supported = inspect.signature(build_graph).parameters
    for name, value in (
        ("executor", executor),
        ("workspace_manager", workspace_manager),
        ("destination", destination),
    ):
        if value is not None and (name in supported or any(p.kind == p.VAR_KEYWORD for p in supported.values())):
            graph_kwargs[name] = value
    graph = build_graph(store.checkpointer(), **graph_kwargs)
    config_dict = {"configurable": {"thread_id": task_id}}
    for chunk in graph.stream(seed, config=config_dict, stream_mode="updates"):
        for node, update in chunk.items():
            status = update.get("status")
            error = update.get("error")
            duration = round(time.monotonic() - node_starts.get(node, time.monotonic()), 1)
            if status:
                print(f"[{_now()}] [{node}] {status} ({duration}s)", flush=True)
                try:
                    store.update_task(
                        task_id,
                        status=status,
                        workspace=(update.get("workspace") or {}).get("path") if isinstance(update.get("workspace"), dict) else update.get("workspace"),
                        branch=(update.get("workspace") or {}).get("branch") if isinstance(update.get("workspace"), dict) else update.get("branch"),
                        pr_number=(update.get("output") or {}).get("provider_state", {}).get("pr_number") if isinstance(update.get("output"), dict) else update.get("pr_number"),
                        error=update.get("error"),
                        input_provider=(update.get("input") or {}).get("provider"),
                        output_provider=(update.get("output") or {}).get("provider"),
                    )
                    workspace.append_event(
                        task_id,
                        event="node_end",
                        node=node,
                        status=status,
                        duration_s=duration,
                    )
                except PersistenceError as exc:
                    print(f"[{_now()}] [{node}] warning: could not persist status: {exc}", flush=True)
            elif error:
                print(f"[{_now()}] [{node}] error: {error}", flush=True)
                try:
                    store.update_task(task_id, error=error)
                    workspace.append_event(
                        task_id,
                        event="node_end",
                        node=node,
                        status=state_mod.FAILED,
                        duration_s=duration,
                        error=error[:200],
                    )
                except PersistenceError as exc:
                    print(f"[{_now()}] [{node}] warning: could not persist error: {exc}", flush=True)
    return graph.get_state(config_dict).values


def _reset_task(store: TaskStore, repository: str, issue_number: int, branch: str) -> None:
    """Remove checkpoints, worktree, and branch so a task can run cleanly from scratch."""
    task_id = f"{repository}#{issue_number}"
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("checkpoints", "writes"):
        if table in tables:
            store.conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (task_id,))
    store.conn.commit()
    ws = workspace.task_workspace(repository, issue_number)
    if ws.exists():
        git.remove_worktree(git.base_repo_dir(repository), ws, branch)
    store.conn.execute(
        """
        UPDATE tasks SET status = ?, workspace = NULL, branch = NULL,
                         pr_number = NULL, error = NULL, updated_at = datetime('now')
        WHERE task_id = ?
        """,
        (state_mod.RECEIVED, task_id),
    )
    store.conn.commit()


def cmd_run(args: argparse.Namespace) -> None:
    repository, issue_number = _parse_ref(args.issue_ref)
    if not config.is_repository_allowed(repository):
        sys.exit(f"repository {repository} is not in the allowlist ({config.CONFIG_FILE})")
    store = TaskStore()
    task_id = f"{repository}#{issue_number}"
    if store.exists(repository, issue_number) and not args.force:
        sys.exit(f"task {task_id} already exists; use 'resume {task_id}' or --force")
    if args.force:
        _reset_task(store, repository, issue_number, f"ai/issue-{issue_number}")
    seed = _seed_state(store, repository, issue_number)
    result = _run_graph(store, seed, task_id)
    _persist_result(store, result)


def _drop_latest_checkpoint(store: TaskStore, task_id: str) -> None:
    """Delete the most recent checkpoint (and its pending writes) so a finished FAILED thread can re-run its last node."""
    store.conn.execute(
        """
        DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id = (
            SELECT checkpoint_id FROM checkpoints
            WHERE thread_id = ? ORDER BY checkpoint_id DESC LIMIT 1
        )
        """,
        (task_id, task_id),
    )
    store.conn.execute("DELETE FROM writes WHERE thread_id = ?", (task_id,))
    store.conn.commit()


def _not_found_hint(task_id: str) -> str:
    return f"task {task_id} not found; run 'orchestrator list' to see task ids (format owner/repo#issue)"


def cmd_resume(args: argparse.Namespace) -> None:
    task_id = args.task_id
    store = TaskStore()
    task = store.get_task(task_id)
    if task is None:
        sys.exit(_not_found_hint(task_id))
    graph = build_graph(store.checkpointer())
    config_dict = {"configurable": {"thread_id": task_id}}
    current = graph.get_state(config_dict).values
    if current.get("status") == state_mod.FAILED:
        _drop_latest_checkpoint(store, task_id)
    result = _run_graph(store, None, task_id)
    _persist_result(store, result)


def cmd_reset(args: argparse.Namespace) -> None:
    """Delete a task entirely so the next execute re-runs it from scratch.

    Standalone and non-running: it only clears DB state (tasks/checkpoints/writes)
    and the worktree/branch. It does not invoke the graph. Because the tasks row is
    removed, the already-running execute picks the issue up again on its next iteration
    via the existing new-issue flow.
    """
    repository, issue_number = _parse_ref(args.issue_ref)
    store = TaskStore()
    task_id = f"{repository}#{issue_number}"
    if store.get_task(task_id) is None:
        sys.exit(_not_found_hint(task_id))
    branch = f"ai/issue-{issue_number}"
    ws = workspace.task_workspace(repository, issue_number)
    if ws.exists():
        try:
            git.remove_worktree(git.base_repo_dir(repository), ws, branch)
        except git.GitError as exc:
            print(f"[{_now()}] reset: warning: could not remove worktree: {exc}", flush=True)
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("checkpoints", "writes"):
        if table in tables:
            store.conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (task_id,))
    store.conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    store.conn.commit()
    print(f"[{_now()}] reset {task_id}: deleted; will re-run on next execute", flush=True)


def _result_pr_number(result: dict) -> int | None:
    """Read the canonical PR number, with a fallback for pre-Task 5 results."""
    output = result.get("output")
    if isinstance(output, dict):
        provider_state = output.get("provider_state")
        if isinstance(provider_state, dict):
            return provider_state.get("pr_number")
        return None
    if "output" not in result:
        return result.get("pr_number")
    return None


def _result_url(result: dict) -> str | None:
    output = result.get("output")
    if isinstance(output, dict):
        url = output.get("url")
        return str(url) if url is not None else None
    return None


def _persist_result(store: TaskStore, result: dict) -> None:
    status = result.get("status", state_mod.FAILED)
    pr_number = _result_pr_number(result)
    publication_url = _result_url(result)
    store.update_task(
        result["task_id"],
        status=status,
        workspace=(result.get("workspace") or {}).get("path") if isinstance(result.get("workspace"), dict) else result.get("workspace"),
        branch=(result.get("workspace") or {}).get("branch") if isinstance(result.get("workspace"), dict) else result.get("branch"),
        pr_number=pr_number,
        publication_url=publication_url,
        error=result.get("error") if status != state_mod.COMPLETED else None,
        input_provider=(result.get("input") or {}).get("provider"),
        output_provider=(result.get("output") or {}).get("provider"),
    )
    if status == state_mod.COMPLETED:
        store.clear_error(result["task_id"])
    store.clear_node(result["task_id"])
    workspace.append_event(
        result["task_id"],
        event="task_end",
        status=status,
        pr_number=pr_number,
        publication_url=publication_url,
        error=(result.get("error") or "")[:200] if status != state_mod.COMPLETED else None,
    )
    if status == state_mod.COMPLETED:
        reference = f"PR #{pr_number}" if pr_number is not None else publication_url or "published result"
        print(f"[{_now()}] COMPLETED: {reference} for {result['task_id']}")
    else:
        print(f"[{_now()}] {status}: {result.get('error', 'no error')}")


def cmd_list(args: argparse.Namespace) -> None:
    store = TaskStore()
    tasks = store.list_tasks()
    if not tasks:
        print("no tasks")
        return
    print(f"{'id (owner/repo#issue)':<28} {'status':<12} {'created_at'}{'pr':>8} error")
    for t in tasks:
        pr = f" #{t['pr_number']}" if t["pr_number"] else f" {t['publication_url']}" if t.get("publication_url") else ""
        err = f" ({t['error']})" if t["error"] else ""
        print(f"{t['task_id']:<28} {t['status']:<12} {t['created_at']}{pr}{err}")


def cmd_status(args: argparse.Namespace) -> None:
    store = TaskStore()
    task = store.get_task(args.task_id)
    if task is None:
        sys.exit(_not_found_hint(args.task_id))
    for key, value in task.items():
        print(f"{key}: {value}")
    print("---")
    for event in workspace.read_events(args.task_id):
        if event.get("event") in ("node_start", "node_end"):
            print(
                f"{event['ts']} {event['event']:<10} {event.get('node',''):<18} "
                f"status={event.get('status','-'):<12} duration={event.get('duration_s','-')}s"
            )


def _format_elapsed(node_started_at: str | None, updated_at: str | None) -> str:
    if not node_started_at:
        return "-"
    try:
        start = datetime.strptime(node_started_at, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(updated_at or node_started_at, "%Y-%m-%d %H:%M:%S")
        seconds = max(0, int((end - start).total_seconds()))
        return f"{seconds // 60}m{seconds % 60:02d}s"
    except ValueError:
        return "-"


def cmd_watch(args: argparse.Namespace) -> None:
    store = TaskStore()
    while True:
        if not args.once:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
        print(f"\n[{_now()}] tasks ({config.LOGS_DIR})")
        for task in store.list_tasks():
            node = task.get("current_node") or ""
            node_elapsed = _format_elapsed(task.get("node_started_at"), task.get("updated_at"))
            log_tail = ""
            if node:
                log_path = workspace.task_log_path(task["task_id"], node)
                if log_path.exists():
                    log_tail = " | ".join(log_path.read_text(errors="replace").splitlines()[-1:])[:100]
            pr = f" PR#{task['pr_number']}" if task.get("pr_number") else ""
            print(
                f"{task['task_id']:<28} {task['status']:<12} "
                f"node={node or '-':<16} {node_elapsed:<8} {task['updated_at']}{pr} {log_tail}"
            )
        if args.once:
            return
        time.sleep(3)


def cmd_logs(args: argparse.Namespace) -> None:
    task_id = args.task_id
    logs_dir = workspace.task_logs_dir(task_id)
    if args.node:
        log_path = logs_dir / f"{args.node}.log"
        if not log_path.exists():
            sys.exit(f"no log for node {args.node!r} in {task_id} ({logs_dir})")
        if args.follow:
            try:
                log_path.touch(exist_ok=True)
                offset = max(0, log_path.stat().st_size - args.lines * 400)
                with log_path.open(errors="replace") as fh:
                    fh.seek(offset)
                    for line in fh:
                        print(line, end="")
                    while True:
                        line = fh.readline()
                        if line:
                            print(line, end="")
                        else:
                            time.sleep(1)
            except KeyboardInterrupt:
                return
        else:
            with log_path.open(errors="replace") as fh:
                lines = fh.readlines()
                for line in lines[-args.lines :]:
                    print(line, end="")
        return
    if not logs_dir.exists():
        sys.exit(f"no logs for {task_id}")
    print(f"logs for {task_id} ({logs_dir}):")
    for path in sorted(logs_dir.glob("*.log")):
        size = path.stat().st_size
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M:%S")
        print(f"  {path.stem:<12} {size:>9,} bytes  last write {mtime}")


def _acquire_poll_lock() -> Path:
    """Single-instance lock for poll: a second poll exits instead of racing tasks."""
    lock_path = config.STATE_DIR / "poll.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_path.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        sys.exit(f"another poll is already running (lock {lock_path})")
    return fd


def _detect_stale_tasks(store: TaskStore) -> None:
    """Mark tasks that look dead (active status, no activity for a long time)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=config.STALE_SECONDS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    rows = store.conn.execute(
        f"SELECT task_id FROM tasks WHERE status IN ({placeholders}) AND updated_at < ?",
        (*ACTIVE_STATUSES, cutoff),
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        print(
            f"[{_now()}] stale task {task_id}: no activity for {config.STALE_SECONDS}s, marking FAILED",
            flush=True,
        )
        store.update_task(task_id, status=state_mod.FAILED, error="process died (stale task)")


def _poll_reviews(review_application) -> None:
    try:
        review_application.poll_once()
    except Exception as exc:
        # Review input providers are independent from issue polling.
        print(f"[{_now()}] review poll error (continuing): {exc}", flush=True)


def cmd_review(args: argparse.Namespace) -> None:
    """Poll configured pull-request reviews without polling issues."""
    lock_fd = _acquire_poll_lock()
    try:
        store = TaskStore()
        review_application = compose_review_runtime(store)
        while True:
            _poll_reviews(review_application)
            if args.once:
                break
            print(f"[{_now()}] review: next check in {config.POLL_INTERVAL_SECONDS}s", flush=True)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        lock_fd.close()


def cmd_execute(args: argparse.Namespace) -> None:
    lock_fd = _acquire_poll_lock()
    try:
        store = TaskStore()
        runtime = compose_runtime(store)
        review_application = compose_review_runtime(store)
        application = PollingApplication(
            store,
            runtime.input_source,
            lambda current_store, seed, task_id: _run_graph(
                current_store,
                seed,
                task_id,
                executor=runtime.executor,
                workspace_manager=runtime.workspace_manager,
                destination=runtime.destination,
            ),
            _persist_result,
            _reset_task,
            now=_now,
            input_provider=runtime.input_provider,
        )
        while True:
            _detect_stale_tasks(store)
            try:
                application.poll_once(args.once)
            except PersistenceError as exc:
                print(f"[{_now()}] execute: persistence error (continuing): {exc}", flush=True)
            _poll_reviews(review_application)
            if args.once:
                break
            print(f"[{_now()}] execute: no new issues, next check in {config.POLL_INTERVAL_SECONDS}s", flush=True)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        lock_fd.close()


ACTIVE_STATUSES = {
    state_mod.RECEIVED,
    state_mod.PREPARING,
    state_mod.PLANNING,
    state_mod.IMPLEMENTING,
    state_mod.TESTING,
    state_mod.CREATING_PR,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orchestrator", description="GitHub Issue -> OpenCode -> PR")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a task for an issue (owner/repo#number)")
    p_run.add_argument("issue_ref")
    p_run.add_argument("--force", action="store_true", help="re-run even if the task exists")
    p_run.set_defaults(func=cmd_run)

    p_execute = sub.add_parser("execute", help="execute issue and review workflows")
    p_execute.add_argument("--once", action="store_true", help="single pass, then exit")
    p_execute.set_defaults(func=cmd_execute)

    p_review = sub.add_parser("review", help="poll configured pull requests for reviews")
    p_review.add_argument("--once", action="store_true", help="single pass, then exit")
    p_review.set_defaults(func=cmd_review)

    p_list = sub.add_parser("list", help="list tasks")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="show task details")
    p_status.add_argument("task_id")
    p_status.set_defaults(func=cmd_status)

    p_resume = sub.add_parser("resume", help="resume an interrupted task")
    p_resume.add_argument("task_id")
    p_resume.set_defaults(func=cmd_resume)

    p_reset = sub.add_parser("reset", help="delete a task so the next execute re-runs it from scratch")
    p_reset.add_argument("issue_ref", help="owner/repo#issue")
    p_reset.set_defaults(func=cmd_reset)

    p_logs = sub.add_parser("logs", help="list or tail a task's node logs")
    p_logs.add_argument("task_id")
    p_logs.add_argument("--node", help="tail a specific node log")
    p_logs.add_argument("--follow", "-f", action="store_true", help="follow the log live")
    p_logs.add_argument("--lines", "-n", type=int, default=50, help="last N lines (default 50)")
    p_logs.set_defaults(func=cmd_logs)

    p_watch = sub.add_parser("watch", help="live view of all tasks")
    p_watch.add_argument("--once", action="store_true", help="single frame, then exit")
    p_watch.set_defaults(func=cmd_watch)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print(
            f"\n[{_now()}] interrupted (Ctrl+C). Running tasks are checkpointed; "
            f"resume later with 'orchestrator resume <task_id>'."
        )
        sys.exit(130)


if __name__ == "__main__":
    main()
