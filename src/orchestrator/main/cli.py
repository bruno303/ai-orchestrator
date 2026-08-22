"""CLI commands for stateless GitHub-backed orchestration."""

from __future__ import annotations

import argparse
import fcntl
import inspect
import re
import sys
import time
from pathlib import Path

from orchestrator.main import config
from orchestrator.infra.filesystem import workspace
from orchestrator.infra.git import client as git
from orchestrator.infra.github import client as github
from orchestrator.infra.langgraph import state as state_mod
from orchestrator.application import PollingApplication, _input_seed
from orchestrator.main.composition import compose_execution_runtime, compose_review_runtime, compose_runtime
from orchestrator.domain import Context, WorkItem
from orchestrator.infra.langgraph.graph import build_graph
from orchestrator.application.ports import InputEvent

ISSUE_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def _parse_ref(ref: str) -> tuple[str, int]:
    match = ISSUE_REF_RE.match(ref)
    if not match:
        sys.exit(f"invalid issue reference {ref!r}; expected owner/repo#number")
    return match.group(1), int(match.group(2))


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _seed_state(repository: str, issue_number: int, *, github_client=github) -> dict:
    issue = github_client.get_issue(repository, issue_number)
    try:
        metadata = github_client.get_repository(repository)
    except github_client.GitHubError:
        metadata = {"ssh_url": f"https://github.com/{repository}.git", "default_branch": ""}
    task_id = f"{repository}#{issue_number}"
    context = Context({"github": {"issue_number": issue_number}, "git": {
        "repository_url": github_client.https_clone_url(metadata, repository),
        "base_branch": metadata.get("default_branch", ""), "branch": f"ai/issue-{issue_number}",
        "workspace": str(workspace.task_workspace(task_id)),
    }})
    event = InputEvent(f"issue:{task_id}", WorkItem(task_id, repository, issue.title, issue.body,
                       input_provider="github", context=context))
    return _input_seed(event, task_id, provider="github")


def _run_graph(seed: dict, task_id: str, *, executor=None, workspace_manager=None,
               destination=None, runtime=None) -> dict:
    """Run an in-memory graph and retain only human-readable event logs."""
    if runtime is None and executor is None and workspace_manager is None and destination is None:
        configured = compose_runtime()
        executor, workspace_manager = configured.executor, configured.workspace_manager
        destination, runtime = configured.destination, configured.execution_runtime
    if runtime is None:
        runtime = compose_execution_runtime(
            executor=executor, workspace_manager=workspace_manager, destination=destination
        )
    starts: dict[str, float] = {}

    def on_node_start(node: str, state: dict) -> None:
        starts[node] = time.monotonic()
        workspace.append_event(task_id, event="node_start", node=node)

    kwargs = {"on_node_start": on_node_start}
    for name, value in (("runtime", runtime),):
        if value is not None and name in inspect.signature(build_graph).parameters:
            kwargs[name] = value
    graph = build_graph(**kwargs)
    # Stream updates are partial; retain the input seed while merging the
    # in-memory execution result instead of querying a durable graph state.
    result: dict = dict(seed)
    for chunk in graph.stream(seed, stream_mode="updates"):
        for node, update in chunk.items():
            result.update(update)
            status, error = update.get("status"), update.get("error")
            duration = round(time.monotonic() - starts.get(node, time.monotonic()), 1)
            if status:
                print(f"[{_now()}] [{node}] {status} ({duration}s)", flush=True)
                workspace.append_event(task_id, event="node_end", node=node, status=status, duration_s=duration)
            elif error:
                print(f"[{_now()}] [{node}] error: {error}", flush=True)
                workspace.append_event(task_id, event="node_end", node=node, status=state_mod.FAILED,
                                       duration_s=duration, error=str(error)[:200])
    return result


def _report_result(result: dict) -> None:
    task_id, status = str(result.get("task_id", "unknown")), result.get("status", state_mod.FAILED)
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    external_id, publication_url = output.get("external_id"), output.get("url")
    workspace.append_event(task_id, event="task_end", status=status, external_id=external_id,
                           publication_url=publication_url,
                           error=str(result.get("error", ""))[:200] if status != state_mod.COMPLETED else None)
    if status == state_mod.COMPLETED:
        print(f"[{_now()}] COMPLETED: {external_id or publication_url or 'published result'} for {task_id}")
    else:
        print(f"[{_now()}] {status}: {result.get('error', 'no error')}")


def _remove_event_workspace(event: InputEvent) -> None:
    context = event.work_item.context.namespace("git")
    path = context.get("workspace")
    if path and Path(str(path)).exists():
        git.remove_worktree(git.base_repo_dir(event.work_item.repository), Path(str(path)), str(context.get("branch", "")))


def _developed_label() -> str:
    """Return the GitHub adapter's configured publication marker."""
    pipeline = config.load_pipeline_config().execution
    return str(pipeline.destination.options.get(
        "developed_label", pipeline.input_source.options.get("developed_label", "ai-developed")
    ))


def cmd_run(args: argparse.Namespace) -> None:
    repository, number = _parse_ref(args.issue_ref)
    if not config.is_repository_allowed(repository):
        sys.exit(f"repository {repository} is not in the allowlist ({config.CONFIG_FILE})")
    runtime = compose_runtime()
    github_client = runtime.input_source.github_client
    issue = github_client.get_issue(repository, number)
    developed_label = _developed_label()
    if developed_label in issue.labels and not args.force:
        sys.exit(f"issue {repository}#{number} is already labeled {developed_label}; use --force to run again")
    _report_result(_run_graph(
        _seed_state(repository, number, github_client=github_client), f"{repository}#{number}",
        executor=runtime.executor, workspace_manager=runtime.workspace_manager,
        destination=runtime.destination, runtime=runtime.execution_runtime,
    ))


def cmd_reset(args: argparse.Namespace) -> None:
    repository, number = _parse_ref(args.issue_ref)
    task_id, branch = f"{repository}#{number}", f"ai/issue-{number}"
    path = workspace.task_workspace(task_id)
    if path.exists():
        try:
            git.remove_worktree(git.base_repo_dir(repository), path, branch)
        except git.GitError as exc:
            print(f"[{_now()}] reset: warning: could not remove worktree: {exc}", flush=True)
    runtime = compose_execution_runtime()
    runtime.destination.github_client.remove_issue_label(repository, number, _developed_label())
    print(f"[{_now()}] reset {task_id}: marker removed; issue is eligible on the next poll", flush=True)


def cmd_logs(args: argparse.Namespace) -> None:
    directory = workspace.task_logs_dir(args.task_id)
    if args.node:
        path = directory / f"{args.node}.log"
        if not path.exists():
            sys.exit(f"no log for node {args.node!r} in {args.task_id} ({directory})")
        print("".join(path.read_text(errors="replace").splitlines(keepends=True)[-args.lines:]), end="")
        return
    if not directory.exists():
        sys.exit(f"no logs for {args.task_id}")
    print(f"logs for {args.task_id} ({directory}):")
    for path in sorted(directory.glob("*.log")):
        print(f"  {path.stem:<12} {path.stat().st_size:>9,} bytes")


def _acquire_poll_lock():
    path = config.STATE_DIR / "poll.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close(); sys.exit(f"another poll is already running (lock {path})")
    return handle


def _poll_reviews(application) -> None:
    try:
        application.poll_once()
    except Exception as exc:
        print(f"[{_now()}] review poll error (continuing): {exc}", flush=True)
    finally:
        print(f"[{_now()}] review poll: finished", flush=True)


def cmd_review(args: argparse.Namespace) -> None:
    lock = _acquire_poll_lock()
    try:
        reviews = compose_review_runtime()
        while True:
            _poll_reviews(reviews)
            if args.once: return
            print(
                f"[{_now()}] review: next check in {config.POLL_INTERVAL_SECONDS}s",
                flush=True,
            )
            time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        lock.close()


def cmd_execute(args: argparse.Namespace) -> None:
    lock = _acquire_poll_lock()
    try:
        runtime, reviews = compose_runtime(), compose_review_runtime()
        application = PollingApplication(runtime.input_source,
            lambda seed, task_id: _run_graph(seed, task_id, executor=runtime.executor,
                workspace_manager=runtime.workspace_manager, destination=runtime.destination, runtime=runtime.execution_runtime),
            _report_result, _remove_event_workspace, now=_now, input_provider=runtime.input_provider,
            feedback=runtime.feedback)
        while True:
            application.poll_once(args.once); _poll_reviews(reviews)
            if args.once: return
            print(f"[{_now()}] execute: no new issues, next check in {config.POLL_INTERVAL_SECONDS}s", flush=True)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        lock.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orchestrator", description="GitHub Issue -> agent -> PR")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a task for an issue (owner/repo#number)")
    run.add_argument("issue_ref"); run.add_argument("--force", action="store_true", help="run even if ai-developed is present"); run.set_defaults(func=cmd_run)
    execute = sub.add_parser("execute", help="execute issue and review workflows")
    execute.add_argument("--once", action="store_true"); execute.set_defaults(func=cmd_execute)
    review = sub.add_parser("review", help="poll configured pull requests for reviews")
    review.add_argument("--once", action="store_true"); review.set_defaults(func=cmd_review)
    reset = sub.add_parser("reset", help="remove local workspace and ai-developed marker")
    reset.add_argument("issue_ref"); reset.set_defaults(func=cmd_reset)
    logs = sub.add_parser("logs", help="list a task's node logs")
    logs.add_argument("task_id"); logs.add_argument("--node"); logs.add_argument("--lines", "-n", type=int, default=50); logs.set_defaults(func=cmd_logs)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nprocess stopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
