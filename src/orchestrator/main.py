"""CLI: run / poll / list / status / resume."""

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
    """Delete a task entirely so the next poll re-runs it from scratch.

    Standalone and non-running: it only clears DB state (tasks/checkpoints/writes)
    and the worktree/branch. It does not invoke the graph. Because the tasks row is
    removed, the already-running poll picks the issue up again on its next iteration
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
    print(f"[{_now()}] reset {task_id}: deleted; will re-run on next poll", flush=True)


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


def _persist_result(store: TaskStore, result: dict) -> None:
    status = result.get("status", state_mod.FAILED)
    pr_number = _result_pr_number(result)
    store.update_task(
        result["task_id"],
        status=status,
        workspace=(result.get("workspace") or {}).get("path") if isinstance(result.get("workspace"), dict) else result.get("workspace"),
        branch=(result.get("workspace") or {}).get("branch") if isinstance(result.get("workspace"), dict) else result.get("branch"),
        pr_number=pr_number,
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
        error=(result.get("error") or "")[:200] if status != state_mod.COMPLETED else None,
    )
    if status == state_mod.COMPLETED:
        print(f"[{_now()}] COMPLETED: PR #{pr_number} for {result['task_id']}")
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
        pr = f" #{t['pr_number']}" if t["pr_number"] else ""
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


def cmd_poll(args: argparse.Namespace) -> None:
    lock_fd = _acquire_poll_lock()
    try:
        store = TaskStore()
        runtime = compose_runtime(store)
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
        )
        while True:
            _detect_stale_tasks(store)
            try:
                application.poll_once(args.once)
            except PersistenceError as exc:
                print(f"[{_now()}] poll: persistence error (continuing): {exc}", flush=True)
            if args.once:
                break
            print(f"[{_now()}] poll: no new issues, next check in {config.POLL_INTERVAL_SECONDS}s", flush=True)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        lock_fd.close()


ACTIVE_STATUSES = {
    state_mod.RECEIVED,
    state_mod.PREPARING,
    state_mod.PLANNING,
    state_mod.IMPLEMENTING,
    state_mod.TESTING,
    state_mod.REVIEWING,
    state_mod.CREATING_PR,
}


def _poll_once(store: TaskStore, once: bool) -> None:
    for repository in config.allowed_repositories():
        label = config.repository_label(repository)
        try:
            issues = github.list_open_issues(repository)
        except github.GitHubError as exc:
            print(f"[{_now()}] poll {repository}: {exc}", flush=True)
            continue

        # 1. /ai-agent comments on open issues (all of them, regardless of label).
        for issue in issues:
            if _check_comments(store, repository, issue.number, f"{repository}#{issue.number}"):
                if once:
                    return

        # 2. PR conversation comments (PRs are issues in the API).
        try:
            prs = github.list_open_pull_requests(repository)
        except github.GitHubError as exc:
            print(f"[{_now()}] poll {repository}: prs: {exc}", flush=True)
            prs = []
        for pr in prs:
            match = re.match(r"^ai/issue-(\d+)$", pr.head_ref)
            if not match:
                continue  # not an orchestrator branch
            issue_number = int(match.group(1))
            if _check_comments(store, repository, pr.number, f"{repository}#{issue_number}", pr_number=pr.number):
                if once:
                    return

        # 3. Label-based new-issue flow (unchanged).
        if label:
            issues = [i for i in issues if label in i.labels]
            print(f"[{_now()}] poll {repository}: {len(issues)} labeled issue(s) (label={label})", flush=True)
        for issue in issues:
            if store.exists(repository, issue.number):
                continue
            print(f"[{_now()}] new issue: {repository}#{issue.number} - {issue.title}")
            seed = {
                "task_id": f"{repository}#{issue.number}",
                "input": {"provider": "github", "data": {"repository": repository, "number": issue.number,
                    "title": issue.title, "body": issue.body}, "provider_state": {}},
                "processing": {}, "workspace": {"branch": f"ai/issue-{issue.number}"}, "output": {},
                "status": state_mod.RECEIVED,
                "iteration": 1,
                "phase_attempts": 1,
            }
            store.create_task(seed["task_id"], repository, issue.number)
            result = _run_graph(store, seed, seed["task_id"])
            _persist_result(store, result)
            if once:
                return


def _pr_context_block(pr: github.PullRequestDetail) -> str:
    files = ", ".join(f"{path} ({status})" for path, status in pr.files[:20]) or "n/a"
    return (
        f"<pr>\n"
        f"number: {pr.number}\n"
        f"title: {pr.title}\n"
        f"url: {pr.url}\n"
        f"base: {pr.base_ref} -> head: {pr.head_ref}\n"
        f"description: {pr.body}\n"
        f"changed files: {files}\n"
        f"</pr>"
    )


def _fetch_pr_context(repository: str, pr_number: int) -> tuple[github.PullRequestDetail | None, str | None]:
    """Fetch PR metadata for the context block; failures are tolerated."""
    try:
        pr = github.get_pull_request(repository, pr_number)
        return pr, _pr_context_block(pr)
    except github.GitHubError as exc:
        print(f"[{_now()}] command comment: could not fetch PR #{pr_number}: {exc}", flush=True)
        return None, None


def _check_comments(
    store: TaskStore, repository: str, number: int, task_id: str, pr_number: int | None = None
) -> bool:
    """Scan comments on an issue or PR conversation for the command prefix.

    Returns True if a comment-triggered run was started.
    """
    command = config.repository_command(repository)
    try:
        comments = github.list_issue_comments(repository, number)
    except github.GitHubError as exc:
        print(f"[{_now()}] poll {repository}#{number}: comments: {exc}", flush=True)
        return False
    triggered = False
    for comment in comments:
        if not comment.body.strip().startswith(command):
            continue
        if store.is_comment_handled(comment.id, stale_after_seconds=config.STALE_SECONDS):
            continue
        task = store.get_task(task_id)
        if task and task["status"] in ACTIVE_STATUSES:
            print(
                f"[{_now()}] poll {repository}#{number}: comment {comment.id} ignored (task {task['status']})",
                flush=True,
            )
            continue
        _trigger_comment_run(store, repository, task_id, comment, pr_number=pr_number)
        triggered = True
    return triggered


def _trigger_comment_run(
    store: TaskStore,
    repository: str,
    task_id: str,
    comment: github.IssueComment,
    pr_number: int | None = None,
) -> None:
    """Full re-run triggered by a /ai-agent comment, with reaction feedback."""
    command = config.repository_command(repository)
    print(
        f"[{_now()}] command comment {comment.id} on {repository} ({task_id}): "
        f"{comment.body.strip().splitlines()[0][:80]}",
        flush=True,
    )

    # Re-check right before resetting: the task may have started since the scan.
    task = store.get_task(task_id)
    if task and task["status"] in ACTIVE_STATUSES:
        print(
            f"[{_now()}] command comment {comment.id}: skipped (task {task_id} is {task['status']})",
            flush=True,
        )
        return

    store.mark_comment_handled(comment.id, task_id, repository, int(task_id.rsplit("#", 1)[1]), "STARTED")
    try:
        github.add_reaction(repository, comment.id, "eyes")
    except github.GitHubError as exc:
        print(f"[{_now()}] reaction 'eyes' failed: {exc}", flush=True)

    issue_number = int(task_id.rsplit("#", 1)[1])
    _reset_task(store, repository, issue_number, f"ai/issue-{issue_number}")

    # Full context: the issue + (when known or when a PR exists) the PR.
    context: list[str] = []
    found_pr: int | None = pr_number
    try:
        issue = github.get_issue(repository, issue_number)
    except github.GitHubError as exc:
        store.update_comment_status(comment.id, state_mod.FAILED)
        try:
            github.add_reaction(repository, comment.id, "-1")
        except github.GitHubError:
            pass
        print(f"[{_now()}] command comment {comment.id}: could not load issue: {exc}", flush=True)
        return

    if found_pr is None:
        try:
            found_pr = github.find_open_pr(repository, f"ai/issue-{issue_number}")
        except github.GitHubError:
            found_pr = None
    if found_pr is not None:
        _, pr_block = _fetch_pr_context(repository, found_pr)
        if pr_block:
            context.append(pr_block)

    context.append(comment.body)
    seed = {
        "task_id": task_id,
        "input": {"provider": "github", "data": {"repository": repository, "number": issue_number,
            "title": issue.title, "body": issue.body, "extra_context": context,
            "pr_number": found_pr}, "provider_state": {}},
        "processing": {}, "workspace": {"branch": f"ai/issue-{issue_number}"}, "output": {},
        "status": state_mod.RECEIVED,
        "iteration": 1,
        "phase_attempts": 1,
    }

    store.create_task(task_id, repository, issue_number)
    result = _run_graph(store, seed, task_id)
    _persist_result(store, result)
    final_status = result.get("status", state_mod.FAILED)
    reaction = "rocket" if final_status == state_mod.COMPLETED else "-1"
    try:
        github.add_reaction(repository, comment.id, reaction)
    except github.GitHubError as exc:
        print(f"[{_now()}] reaction '{reaction}' failed: {exc}", flush=True)
    store.update_comment_status(comment.id, final_status)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orchestrator", description="GitHub Issue -> OpenCode -> PR")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a task for an issue (owner/repo#number)")
    p_run.add_argument("issue_ref")
    p_run.add_argument("--force", action="store_true", help="re-run even if the task exists")
    p_run.set_defaults(func=cmd_run)

    p_poll = sub.add_parser("poll", help="poll allowed repos for new issues")
    p_poll.add_argument("--once", action="store_true", help="single pass, then exit")
    p_poll.set_defaults(func=cmd_poll)

    p_list = sub.add_parser("list", help="list tasks")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="show task details")
    p_status.add_argument("task_id")
    p_status.set_defaults(func=cmd_status)

    p_resume = sub.add_parser("resume", help="resume an interrupted task")
    p_resume.add_argument("task_id")
    p_resume.set_defaults(func=cmd_resume)

    p_reset = sub.add_parser("reset", help="delete a task so the next poll re-runs it from scratch")
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
