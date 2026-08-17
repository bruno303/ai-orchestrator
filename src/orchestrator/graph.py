"""LangGraph workflow: Issue -> workspace -> plan -> implement -> test -> review -> PR."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from orchestrator import config, opencode, state as state_mod, workspace
from orchestrator.github_destination import GitHubDestination
from orchestrator.git_workspace import GitWorkspaceManager
from orchestrator.providers import (
    Destination, ExecutionRequest, ExecutionResult, Executor, PublicationRequest,
    WorkspaceManager, WorkspaceRequest, WorkspaceResult, validate_provider_state,
)
from orchestrator.state import TaskState

PLAN_FILE = ".agents/plans/plan.md"


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _clean_terminal_output(text: str) -> str:
    """Remove terminal control sequences before persisting model output."""
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")


def _fail(state: TaskState, error: str) -> dict[str, Any]:
    return {"status": state_mod.FAILED, "error": error}


def _input(state: TaskState) -> dict[str, Any]:
    value = state.get("input") or {}
    data = value.get("data") or {}
    provider_state = validate_provider_state(value.get("provider_state", {}))
    return {
        "provider": value.get("provider", "github"),
        "provider_state": provider_state,
        "repository": data.get("repository", state.get("repository", "")),
        "issue_number": data.get("number", data.get("issue_number", state.get("issue_number"))),
        "issue_title": data.get("title", data.get("issue_title", state.get("issue_title", ""))),
        "issue_body": data.get("body", data.get("issue_body", state.get("issue_body", ""))),
        "extra_context": data.get("extra_context", state.get("extra_context", [])),
    }


def _processing(state: TaskState, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(state.get("processing") or {})
    if updates:
        result.update(updates)
    for key in ("plan_path", "plan_summary", "implementation_result", "test_result", "review_result", "review_verdict"):
        if key not in result and state.get(key) is not None:
            result[key] = state[key]
    return result


def _workspace(state: TaskState) -> dict[str, Any]:
    value = state.get("workspace") or {}
    if not isinstance(value, dict):
        value = {"path": value}
    result = dict(value)
    fallbacks = (("path", "workspace_path"), ("branch", "branch"), ("base_branch", "base_branch"),
                 ("provider_state", "provider_state"))
    for key, legacy in fallbacks:
        if key not in result and state.get(legacy) is not None and not isinstance(state.get(legacy), dict):
            result[key] = state[legacy]
    result.setdefault("path", "")
    result.setdefault("branch", "")
    result.setdefault("base_branch", "")
    if "provider_state" not in result:
        result["provider_state"] = {}
    return result


def _provider_name(component: Any, default: str) -> str:
    return str(getattr(component, "provider_type", getattr(component, "provider_name", default)))


def _namespace_updates(state: TaskState, *, input_data: dict[str, Any] | None = None,
                      processing: dict[str, Any] | None = None,
                      workspace_data: dict[str, Any] | None = None,
                      output: dict[str, Any] | None = None) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if input_data is not None:
        updates["input"] = input_data
    if processing is not None:
        updates["processing"] = processing
    if workspace_data is not None:
        updates["workspace"] = workspace_data
    if output is not None:
        updates["output"] = output
    return updates


def _run_opencode(
    state: TaskState, node: str, agent: str, prompt: str, executor: Executor | None = None
) -> tuple[dict[str, Any], ExecutionResult | None]:
    executor = executor or opencode.OpenCodeExecutor()
    log_path = workspace.task_log_path(state["task_id"], node)
    # Per-phase attempt budget: each phase starts at 1 regardless of retries in
    # earlier phases. phase_attempts is written to state for observability only
    # and must never feed back into the counter (state is shared across nodes).
    attempt = 1
    max_attempts = config.PHASE_MAX_ATTEMPTS if config.MODEL_FALLBACK_ENABLED else 1
    while attempt <= max_attempts:
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
        previous_provider_state = validate_provider_state(
            _processing(state).get("provider_state", {})
        )
        try:
            result = executor.execute(
                ExecutionRequest(
                    task_id=state["task_id"],
                    workspace=_workspace(state)["path"],
                    prompt=prompt,
                    agent=agent,
                    model=model,
                    variant=variant,
                    provider_state={
                        **previous_provider_state,
                        "log_file": str(log_path),
                        "detect_degenerate": config.MODEL_FALLBACK_ENABLED,
                    },
                )
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
            if attempt > max_attempts:
                return (
                    {
                        **_fail(
                            state,
                            f"{node} produced degenerate output after {max_attempts} attempts",
                        ),
                        "phase_attempts": max_attempts,
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
        try:
            provider_state = validate_provider_state(
                {**previous_provider_state, **result.provider_state}
            )
        except TypeError as exc:
            return {**_fail(state, str(exc)), "phase_attempts": attempt}, None
        print(
            f"[{_now()}] {node}: finished in {result.duration_seconds:.0f}s "
            f"(exit={result.exit_code}, attempt={attempt}, model={model_label}, variant={variant_label})",
            flush=True,
        )
        if not result.success or result.exit_code != 0:
            error = (
                f"{node} executor ({agent}) reported failure"
                if result.exit_code == 0
                else f"opencode ({agent}) exited with {result.exit_code}"
            )
            return (
                {
                    **_fail(state, error),
                    "phase_attempts": attempt,
                    "processing": _processing(state, {"provider_state": provider_state}),
                },
                result,
            )
        return {"phase_attempts": attempt, "processing": _processing(state, {"provider_state": provider_state})}, result
    return (
        {
            **_fail(
                state,
                f"{node} produced degenerate output after {max_attempts} attempts",
            ),
            "phase_attempts": max_attempts,
        },
        None,
    )


# ---------------------------------------------------------------- nodes


def prepare_workspace(state: TaskState, manager: WorkspaceManager | None = None) -> dict[str, Any]:
    print(f"[{_now()}] prepare_workspace: starting", flush=True)
    source = _input(state)
    repository = source["repository"]
    if not config.is_repository_allowed(repository):
        return _fail(state, f"repository {repository} is not in the allowlist")
    try:
        current_workspace = _workspace(state)
        branch = current_workspace["branch"] or f"ai/issue-{source['issue_number']}"
        ws = str(current_workspace["path"] or workspace.task_workspace(repository, source["issue_number"]))
        result = (manager or GitWorkspaceManager()).prepare(
            WorkspaceRequest(
                task_id=state["task_id"], repository=repository, branch=branch,
                base_branch=current_workspace["base_branch"],
                provider_state={"repository_url": source["provider_state"].get("repository_url", state.get("repository_url")),
                                "workspace": ws},
            )
        )
        provider_state = validate_provider_state(result.provider_state)
        resolved_base_branch = provider_state.get("base_branch", state.get("base_branch", ""))
    except Exception as exc:
        print(f"[{_now()}] prepare_workspace: ERROR {exc}", flush=True)
        return _fail(state, str(exc))
    print(
        f"[{_now()}] prepare_workspace: workspace={result.workspace} branch={result.branch} base={resolved_base_branch}",
        flush=True,
    )
    input_value = state.get("input") or {}
    input_data = dict(input_value.get("data") or {})
    input_data.update({"repository": source["repository"], "number": source["issue_number"],
                       "title": source["issue_title"], "body": source["issue_body"],
                       "extra_context": source["extra_context"]})
    return {
        **_namespace_updates(
            state,
            input_data={"provider": source["provider"], "provider_state": source["provider_state"],
                        "data": input_data},
            workspace_data={"provider": _provider_name(manager, "git"), "path": result.workspace, "branch": result.branch,
                            "base_branch": resolved_base_branch,
                            "provider_state": provider_state},
        ),
        "status": state_mod.PREPARING,
    }


def plan(state: TaskState, executor: Executor | None = None) -> dict[str, Any]:
    prompt = plan_prompt(state)
    updates, result = _run_opencode(state, "plan", "plan", prompt, executor)
    if updates.get("status") == state_mod.FAILED:
        return updates
    plan_path = Path(_workspace(state)["path"]) / PLAN_FILE
    if not plan_path.is_file() and result and result.stdout.strip():
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(_clean_terminal_output(result.stdout).strip() + "\n")
    if not plan_path.is_file():
        return _fail(state, f"planning completed without creating {PLAN_FILE}")
    if not plan_path.read_text().strip():
        return _fail(state, f"planning completed with an empty {PLAN_FILE}")
    processing = {**_processing(state, updates.get("processing")), "plan_path": PLAN_FILE, "plan_summary": result.stdout[:4000] if result else None}
    return {
        **updates,
        "status": state_mod.PLANNING,
        "processing": processing,
    }


def implement(state: TaskState, executor: Executor | None = None) -> dict[str, Any]:
    updates, result = _run_opencode(state, "implement", "build", implement_prompt(state), executor)
    if updates.get("status") == state_mod.FAILED:
        return updates
    processing = {**_processing(state, updates.get("processing")), "implementation_result": result.stdout[:4000] if result else None}
    return {
        **updates,
        "status": state_mod.IMPLEMENTING,
        "processing": processing,
    }


def test(state: TaskState, executor: Executor | None = None) -> dict[str, Any]:
    updates, result = _run_opencode(state, "test", "build", test_prompt(state), executor)
    if updates.get("status") == state_mod.FAILED:
        return updates
    processing = {**_processing(state, updates.get("processing")), "test_result": result.stdout[:4000] if result else None}
    return {
        **updates,
        "status": state_mod.TESTING,
        "processing": processing,
    }


def review(state: TaskState, executor: Executor | None = None) -> dict[str, Any]:
    updates, result = _run_opencode(state, "review", "plan", review_prompt(state), executor)
    if updates.get("status") == state_mod.FAILED:
        return updates
    verdict = parse_verdict(result.stdout) if result else state_mod.VERDICT_NEEDS_CLARIFICATION
    processing = {**_processing(state, updates.get("processing")), "review_result": result.stdout[:4000] if result else None, "review_verdict": verdict}
    return {
        **updates,
        "status": state_mod.REVIEWING,
        "processing": processing,
    }


def create_pr(state: TaskState, destination: Destination | None = None) -> dict[str, Any]:
    print(f"[{_now()}] create_pr: starting", flush=True)
    try:
        source = _input(state)
        processing = _processing(state)
        workspace_state = _workspace(state)
        title = f"feat: {source['issue_title']}"[:72]
        result = (destination or GitHubDestination()).publish(
            PublicationRequest(
                repository=source["repository"], title=title, body=_pr_body(state),
                head=workspace_state["branch"], base=workspace_state["base_branch"],
                provider_state={
                    "workspace": workspace_state["path"], "issue_number": source["issue_number"],
                    "review_verdict": processing.get("review_verdict"), "review_result": processing.get("review_result"),
                },
            )
        )
        pr_number = result.number
    except Exception as exc:
        print(f"[{_now()}] create_pr: ERROR {exc}", flush=True)
        return _fail(state, str(exc))
    print(f"[{_now()}] create_pr: PR #{pr_number} created", flush=True)
    publication_state = validate_provider_state(result.provider_state)
    output = dict(state.get("output") or {})
    output.update({"provider": _provider_name(destination, "github"),
                   "provider_state": {**publication_state, "pr_number": pr_number}})
    return {"status": state_mod.COMPLETED, "output": output}


def cleanup(state: TaskState, manager: WorkspaceManager | None = None) -> dict[str, Any]:
    """Remove the task worktree and branch after the PR was created.

    Runs only on success; failed tasks keep their worktree for debugging.
    Cleanup problems are logged but never fail the task.
    """
    print(f"[{_now()}] cleanup: starting", flush=True)
    try:
        cleanup_manager = manager or GitWorkspaceManager()
        workspace_state = _workspace(state)
        provider_state = dict(workspace_state.get("provider_state") or {})
        if isinstance(cleanup_manager, GitWorkspaceManager) and "repository" not in provider_state:
            provider_state["repository"] = _input(state)["repository"]
        cleanup_manager.cleanup(
            WorkspaceResult(
                workspace=workspace_state["path"], branch=workspace_state["branch"], provider_state=provider_state,
            )
        )
    except Exception as exc:
        print(f"[{_now()}] cleanup: ERROR {exc}", flush=True)
        return {"status": state_mod.COMPLETED}
    print(f"[{_now()}] cleanup: removed {_workspace(state)['path']} (branch {_workspace(state)['branch']})", flush=True)
    return {"status": state_mod.COMPLETED}


# ---------------------------------------------------------------- prompts


def _extra_context(state: TaskState) -> str:
    extra_context = _input(state)["extra_context"]
    if not extra_context:
        return ""
    blocks = "\n\n".join(f"<comment>\n{text}\n</comment>" for text in extra_context)
    return f"\n\nAdditional requirements from comments:\n{blocks}"


def plan_prompt(state: TaskState) -> str:
    source = _input(state)
    return f"""You are planning the implementation of GitHub issue #{source['issue_number']} in repository {source['repository']}.

Issue title: {source['issue_title']}

Issue body:
{source['issue_body']}
{_extra_context(state)}
Use the plan-implementation skill.

Requirements:
1. Analyze the issue and the repository.
2. Produce the implementation plan in your final response. The orchestrator
   will save your response to {PLAN_FILE} in this workspace. The plan must use
   the format required by the subagent-plan-execution skill:
   clearly separated tasks, each with **Files:** and **Dependencies:**, detailed enough for an implementer unfamiliar with the project.
3. The plan must cover: requirements, implementation steps, files likely to change, tests required, potential risks, and open questions if the requirements are ambiguous.

Restrictions:
- Do NOT modify or create repository files. Planning only.
- Do NOT create a pull request.
- Do NOT run tests or builds.
"""


def implement_prompt(state: TaskState) -> str:
    source = _input(state)
    return f"""You are implementing GitHub issue #{source['issue_number']} in repository {source['repository']}.

Issue title: {source['issue_title']}

Issue body:
{source['issue_body']}
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
    source = _input(state)
    return f"""Run the test suite of the project in this workspace (repository {source['repository']}, issue #{source['issue_number']}).

Determine the appropriate test command (e.g. pytest, npm test, ./gradlew test) and run it.
Do NOT modify any code.
Report the results, including any failures.
"""


def review_prompt(state: TaskState) -> str:
    source = _input(state)
    workspace_state = _workspace(state)
    return f"""Review the implementation for GitHub issue #{source['issue_number']} in repository {source['repository']}.

Issue title: {source['issue_title']}

Issue body:
{source['issue_body']}
{_extra_context(state)}
Plan: {PLAN_FILE}
Implementation: git diff of the current branch ({workspace_state['branch']}) against origin/{workspace_state['base_branch']}.

Use the review-changes skill.

Analyze: adherence to the issue requirements, adherence to the plan, code quality, test coverage, and possible regressions.

End your response with exactly this machine-readable block and nothing else:
REVIEW_STATUS: APPROVED|CHANGES_REQUIRED|NEEDS_CLARIFICATION
FINDINGS:
- only the unapproved findings, one per bullet

If the review is APPROVED, omit the FINDINGS block entirely.

Do NOT modify any files.
"""


# ---------------------------------------------------------------- helpers


def parse_verdict(output: str) -> str:
    match = re.search(r"(?:REVIEW_STATUS|VERDICT):\s*(APPROVED|CHANGES_REQUIRED|NEEDS_CLARIFICATION)", output)
    return match.group(1) if match else state_mod.VERDICT_NEEDS_CLARIFICATION


def _review_findings(output: str) -> list[str]:
    findings: list[str] = []
    in_findings = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if in_findings:
                continue
            continue
        if line.startswith("FINDINGS:"):
            in_findings = True
            continue
        if re.match(r"^[A-Z_]+:\s*", line):
            if in_findings:
                break
            continue
        if in_findings and line.startswith("- "):
            findings.append(line[2:].strip())
    return findings


def _review_section(state: TaskState) -> str | None:
    """Markdown section for a non-approved review, or None if there is nothing to flag."""
    processing = _processing(state)
    verdict = processing.get("review_verdict")
    result = processing.get("review_result")
    if not verdict or verdict == state_mod.VERDICT_APPROVED or not result:
        return None
    findings = _review_findings(result)
    if not findings:
        return None
    lines = ["## Last Review report", f"- status: {verdict}", "- findings:"]
    lines.extend(f"  - {finding}" for finding in findings)
    return "\n".join(lines)


def _pr_body(state: TaskState, current_body: str | None = None) -> str:
    """PR body: `Closes #n` first, then review sections (most recent to oldest).

    The `Closes #n` line is always the first line; a leading `Closes #n` line
    in `current_body` is stripped so it is never duplicated. All other text in
    `current_body` is kept below the new section (history preserved).
    """
    closes = f"Closes #{_input(state)['issue_number']}"
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


def build_graph(
    checkpointer: Any | None = None,
    on_node_start: Callable[[str, TaskState], None] | None = None,
    executor: Executor | None = None,
    workspace_manager: WorkspaceManager | None = None,
    destination: Destination | None = None,
):
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
    phase_executor = executor or opencode.OpenCodeExecutor()
    nodes = {
        "prepare_workspace": lambda state: prepare_workspace(state, workspace_manager),
        "plan": lambda state: plan(state, phase_executor),
        "implement": lambda state: implement(state, phase_executor),
        "test": lambda state: test(state, phase_executor),
        "review": lambda state: review(state, phase_executor),
        "create_pr": lambda state: create_pr(state, destination),
        "cleanup": lambda state: cleanup(state, workspace_manager),
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
