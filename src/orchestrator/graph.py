"""LangGraph workflow: Issue -> workspace -> plan -> implement -> test -> review -> PR."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from orchestrator import config, git, github, opencode, state as state_mod, workspace
from orchestrator.state import TaskState

PLAN_FILE = ".agents/plans/plan.md"


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fail(state: TaskState, error: str) -> dict[str, Any]:
    return {"status": state_mod.FAILED, "error": error}


def _run_opencode(
    state: TaskState, node: str, agent: str, prompt: str
) -> tuple[dict[str, Any], opencode.OpenCodeResult | None]:
    log_path = workspace.task_log_path(state["task_id"], node)
    # Per-phase attempt budget: each phase starts at 1 regardless of retries in
    # earlier phases. phase_attempts is written to state for observability only
    # and must never feed back into the counter (state is shared across nodes).
    attempt = 1
    while attempt <= config.PHASE_MAX_ATTEMPTS:
        model_cfg = config.MODEL_PRIMARY if attempt == 1 else config.MODEL_FALLBACK
        model = model_cfg.name if model_cfg else None
        variant = model_cfg.variant if model_cfg else None
        model_label = model or "default"
        variant_label = variant or "-"
        print(
            f"[{_now()}] {node}: starting opencode (agent={agent}, attempt={attempt}, "
            f"model={model_label}, variant={variant_label}, log={log_path})",
            flush=True,
        )
        try:
            result = opencode.run_opencode(
                workspace=state["workspace"],
                agent=agent,
                prompt=prompt,
                log_file=log_path,
                model=model,
                variant=variant,
            )
        except opencode.DegenerateOutputError:
            next_cfg = config.MODEL_FALLBACK
            next_model = next_cfg.name if next_cfg else None
            next_variant = next_cfg.variant if next_cfg else None
            print(
                f"[{_now()}] {node}: degenerate output (attempt={attempt}, model={model_label}, "
                f"variant={variant_label}), retrying with model={next_model or 'default'}, "
                f"variant={next_variant or '-'}",
                flush=True,
            )
            attempt += 1
            if attempt > config.PHASE_MAX_ATTEMPTS:
                return (
                    {
                        **_fail(
                            state,
                            f"{node} produced degenerate output after {config.PHASE_MAX_ATTEMPTS} attempts",
                        ),
                        "phase_attempts": config.PHASE_MAX_ATTEMPTS,
                    },
                    None,
                )
            continue
        except opencode.OpenCodeError as exc:
            print(
                f"[{_now()}] {node}: ERROR {exc} "
                f"(attempt={attempt}, model={model_label}, variant={variant_label})",
                flush=True,
            )
            return {**_fail(state, str(exc)), "phase_attempts": attempt}, None
        print(
            f"[{_now()}] {node}: finished in {result.duration_seconds:.0f}s "
            f"(exit={result.exit_code}, attempt={attempt}, model={model_label}, variant={variant_label})",
            flush=True,
        )
        if result.exit_code != 0:
            return (
                {
                    **_fail(state, f"opencode ({agent}) exited with {result.exit_code}"),
                    "phase_attempts": attempt,
                },
                result,
            )
        return {"phase_attempts": attempt}, result
    return (
        {
            **_fail(
                state,
                f"{node} produced degenerate output after {config.PHASE_MAX_ATTEMPTS} attempts",
            ),
            "phase_attempts": config.PHASE_MAX_ATTEMPTS,
        },
        None,
    )


# ---------------------------------------------------------------- nodes


def prepare_workspace(state: TaskState) -> dict[str, Any]:
    print(f"[{_now()}] prepare_workspace: starting", flush=True)
    repository = state["repository"]
    if not config.is_repository_allowed(repository):
        return _fail(state, f"repository {repository} is not in the allowlist")
    try:
        if not state.get("repository_url"):
            state["repository_url"] = github.get_clone_url(repository)
        if not state.get("base_branch"):
            state["base_branch"] = github.get_default_branch(repository)
        repo_dir = git.ensure_base_clone(repository, state["repository_url"])
        if not state.get("base_branch"):
            state["base_branch"] = git.detect_default_branch(repo_dir)
        branch = state["branch"] or f"ai/issue-{state['issue_number']}"
        ws = str(state.get("workspace") or workspace.task_workspace(repository, state["issue_number"]))
        git.create_worktree(repo_dir, Path(ws), branch, state["base_branch"])
    except (git.GitError, github.GitHubError) as exc:
        print(f"[{_now()}] prepare_workspace: ERROR {exc}", flush=True)
        return _fail(state, str(exc))
    print(
        f"[{_now()}] prepare_workspace: workspace={ws} branch={branch} base={state['base_branch']}",
        flush=True,
    )
    return {
        "repository_url": state["repository_url"],
        "base_branch": state["base_branch"],
        "branch": branch,
        "workspace": ws,
        "status": state_mod.PREPARING,
    }


def plan(state: TaskState) -> dict[str, Any]:
    prompt = plan_prompt(state)
    updates, result = _run_opencode(state, "plan", "plan", prompt)
    if updates.get("status") == state_mod.FAILED:
        return updates
    return {
        **updates,
        "plan_path": PLAN_FILE,
        "plan_summary": result.stdout[:4000] if result else None,
        "status": state_mod.PLANNING,
    }


def implement(state: TaskState) -> dict[str, Any]:
    updates, result = _run_opencode(state, "implement", "build", implement_prompt(state))
    if updates.get("status") == state_mod.FAILED:
        return updates
    return {
        **updates,
        "implementation_result": result.stdout[:4000] if result else None,
        "status": state_mod.IMPLEMENTING,
    }


def test(state: TaskState) -> dict[str, Any]:
    updates, result = _run_opencode(state, "test", "build", test_prompt(state))
    if updates.get("status") == state_mod.FAILED:
        return updates
    return {
        **updates,
        "test_result": result.stdout[:4000] if result else None,
        "status": state_mod.TESTING,
    }


def review(state: TaskState) -> dict[str, Any]:
    updates, result = _run_opencode(state, "review", "plan", review_prompt(state))
    if updates.get("status") == state_mod.FAILED:
        return updates
    verdict = parse_verdict(result.stdout) if result else state_mod.VERDICT_NEEDS_CLARIFICATION
    return {
        **updates,
        "review_result": result.stdout[:4000] if result else None,
        "review_verdict": verdict,
        "status": state_mod.REVIEWING,
    }


def create_pr(state: TaskState) -> dict[str, Any]:
    print(f"[{_now()}] create_pr: starting", flush=True)
    ws = state["workspace"]
    base = state["base_branch"]
    branch = state["branch"]
    repository = state["repository"]
    try:
        if not git.has_changes(ws) and not git.commits_ahead(ws, base):
            print(f"[{_now()}] create_pr: no changes to commit", flush=True)
            return _fail(state, "no changes to commit")
        title = f"feat: {state['issue_title']}"[:72]
        commit_body = f"Closes #{state['issue_number']}"
        if git.has_changes(ws):
            git.commit_all(ws, f"{title}\n\n{commit_body}")
        git.push_branch(ws, branch)
        existing_pr = github.find_open_pr(repository, branch)
        if existing_pr is not None:
            pr_number = existing_pr
            print(f"[{_now()}] create_pr: reusing existing PR #{pr_number} (force-pushed)", flush=True)
            current_body = github.get_pull_request(repository, pr_number).body
            new_body = _pr_body(state, current_body=current_body)
            if new_body != current_body:
                github.update_pull_request_body(repository, pr_number, new_body)
        else:
            pr_number = github.create_pull_request(
                repository, title, _pr_body(state), head=branch, base=base
            )
    except (git.GitError, github.GitHubError) as exc:
        print(f"[{_now()}] create_pr: ERROR {exc}", flush=True)
        return _fail(state, str(exc))
    print(f"[{_now()}] create_pr: PR #{pr_number} created", flush=True)
    return {"pr_number": pr_number, "status": state_mod.COMPLETED}


def cleanup(state: TaskState) -> dict[str, Any]:
    """Remove the task worktree and branch after the PR was created.

    Runs only on success; failed tasks keep their worktree for debugging.
    Cleanup problems are logged but never fail the task.
    """
    print(f"[{_now()}] cleanup: starting", flush=True)
    ws = state["workspace"]
    branch = state["branch"]
    repository = state["repository"]
    try:
        git.remove_worktree(git.base_repo_dir(repository), Path(ws), branch)
    except git.GitError as exc:
        print(f"[{_now()}] cleanup: ERROR {exc}", flush=True)
        return {"status": state_mod.COMPLETED}
    if Path(ws).exists():
        shutil.rmtree(Path(ws), ignore_errors=True)
    print(f"[{_now()}] cleanup: removed {ws} (branch {branch})", flush=True)
    return {"status": state_mod.COMPLETED}


# ---------------------------------------------------------------- prompts


def _extra_context(state: TaskState) -> str:
    if not state.get("extra_context"):
        return ""
    blocks = "\n\n".join(f"<comment>\n{text}\n</comment>" for text in state["extra_context"])
    return f"\n\nAdditional requirements from comments:\n{blocks}"


def plan_prompt(state: TaskState) -> str:
    return f"""You are planning the implementation of GitHub issue #{state['issue_number']} in repository {state['repository']}.

Issue title: {state['issue_title']}

Issue body:
{state['issue_body']}
{_extra_context(state)}
Use the plan-implementation skill.

Requirements:
1. Analyze the issue and the repository.
2. Write the implementation plan to {PLAN_FILE} in this workspace.
   The plan must use the format required by the subagent-plan-execution skill:
   clearly separated tasks, each with **Files:** and **Dependencies:**, detailed enough for an implementer unfamiliar with the project.
3. The plan must cover: requirements, implementation steps, files likely to change, tests required, potential risks, and open questions if the requirements are ambiguous.

Restrictions:
- Do NOT modify any repository files. Planning only.
- Do NOT create a pull request.
- Do NOT run tests or builds.
"""


def implement_prompt(state: TaskState) -> str:
    return f"""You are implementing GitHub issue #{state['issue_number']} in repository {state['repository']}.

Issue title: {state['issue_title']}

Issue body:
{state['issue_body']}
{_extra_context(state)}
A plan has been written to {PLAN_FILE}.

Execute the plan using the subagent-plan-execution skill. Invoke it explicitly: load the skill "subagent-plan-execution" (base directory: {config.SKILL_SUBAGENT_PLAN_EXECUTION}) and follow its steps exactly:
- Step 0: read {PLAN_FILE}
- Step 1: for each task, write the brief file, dispatch a fresh implementer subagent, then dispatch a fresh reviewer subagent
- Step 2: run the quality gate (build, tests, lint) and fix any failures

Context:
- Work only inside this workspace (the task worktree).
- Do NOT create a pull request or push anything.
- Report the final result when done.
"""


def test_prompt(state: TaskState) -> str:
    return f"""Run the test suite of the project in this workspace (repository {state['repository']}, issue #{state['issue_number']}).

Determine the appropriate test command (e.g. pytest, npm test, ./gradlew test) and run it.
Do NOT modify any code.
Report the results, including any failures.
"""


def review_prompt(state: TaskState) -> str:
    return f"""Review the implementation for GitHub issue #{state['issue_number']} in repository {state['repository']}.

Issue title: {state['issue_title']}

Issue body:
{state['issue_body']}
{_extra_context(state)}
Plan: {PLAN_FILE}
Implementation: git diff of the current branch ({state['branch']}) against origin/{state['base_branch']}.

Use the review-changes skill.

Analyze: adherence to the issue requirements, adherence to the plan, code quality, test coverage, and possible regressions.

End your response with exactly one verdict line:
VERDICT: APPROVED
VERDICT: CHANGES_REQUIRED
VERDICT: NEEDS_CLARIFICATION

Do NOT modify any files.
"""


# ---------------------------------------------------------------- helpers


def parse_verdict(output: str) -> str:
    match = re.search(r"VERDICT:\s*(APPROVED|CHANGES_REQUIRED|NEEDS_CLARIFICATION)", output)
    return match.group(1) if match else state_mod.VERDICT_NEEDS_CLARIFICATION


def _review_section(state: TaskState) -> str | None:
    """Markdown section for a non-approved review, or None if there is nothing to flag."""
    verdict = state.get("review_verdict")
    result = state.get("review_result")
    if not verdict or verdict == state_mod.VERDICT_APPROVED or not result:
        return None
    return f"## Review: {verdict}\n\n{result.rstrip()}"


def _pr_body(state: TaskState, current_body: str | None = None) -> str:
    """PR body: `Closes #n` first, then review sections (most recent to oldest).

    The `Closes #n` line is always the first line; a leading `Closes #n` line
    in `current_body` is stripped so it is never duplicated. All other text in
    `current_body` is kept below the new section (history preserved).
    """
    closes = f"Closes #{state['issue_number']}"
    section = _review_section(state)
    if section is None:
        return current_body if current_body else closes
    remainder = ""
    if current_body:
        remainder = current_body.lstrip("\n")
        if remainder.startswith(closes):
            remainder = remainder[len(closes):].lstrip("\n")
    if remainder:
        return f"{closes}\n\n{section}\n\n{remainder}"
    return f"{closes}\n\n{section}"


# ---------------------------------------------------------------- graph


def _route(next_node: str) -> Callable[[TaskState], str]:
    def route(state: TaskState) -> str:
        return "end" if state.get("status") == state_mod.FAILED else next_node

    return route


def build_graph(checkpointer: Any | None = None, on_node_start: Callable[[str, TaskState], None] | None = None):
    """Build the workflow graph.

    `on_node_start(node_name, state)` is invoked when each node starts (heartbeat).
    Nodes are guarded: any unhandled exception becomes a FAILED state instead of
    crashing the process.
    """

    def _guard(node_name: str, fn: Callable[[TaskState], dict[str, Any]]) -> Callable[[TaskState], dict[str, Any]]:
        def wrapped(state: TaskState) -> dict[str, Any]:
            if on_node_start is not None:
                try:
                    on_node_start(node_name, state)
                except Exception:
                    pass
            try:
                return fn(state)
            except Exception as exc:
                print(
                    f"[{_now()}] {node_name}: ERROR (unhandled) {type(exc).__name__}: {exc}",
                    flush=True,
                )
                return {"status": state_mod.FAILED, "error": f"unhandled exception in {node_name}: {exc}"}

        return wrapped

    builder = StateGraph(TaskState)
    nodes = {
        "prepare_workspace": prepare_workspace,
        "plan": plan,
        "implement": implement,
        "test": test,
        "review": review,
        "create_pr": create_pr,
        "cleanup": cleanup,
    }
    for name, fn in nodes.items():
        builder.add_node(name, _guard(name, fn))

    builder.add_edge(START, "prepare_workspace")
    builder.add_conditional_edges("prepare_workspace", _route("plan"), {"plan": "plan", "end": END})
    builder.add_conditional_edges("plan", _route("implement"), {"implement": "implement", "end": END})
    builder.add_conditional_edges("implement", _route("test"), {"test": "test", "end": END})
    builder.add_conditional_edges("test", _route("review"), {"review": "review", "end": END})
    builder.add_conditional_edges("review", _route("create_pr"), {"create_pr": "create_pr", "end": END})
    builder.add_conditional_edges("create_pr", _route("cleanup"), {"cleanup": "cleanup", "end": END})
    builder.add_edge("cleanup", END)

    return builder.compile(checkpointer=checkpointer)
