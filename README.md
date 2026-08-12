# AI Orchestrator

Local orchestrator (LangGraph + OpenCode) that turns GitHub Issues into pull requests:

```
GitHub Issue → workspace (git worktree) → OpenCode plan → OpenCode build (subagent-plan-execution)
             → tests → OpenCode review → PR
```

## Requirements

- `uv`, `git`, `opencode` (>= 1.18), `gh` (authenticated with `repo` scope)

## Setup

```bash
uv sync
```

## Configuration

Edit `config/repositories.yaml` — only repositories listed here can trigger the
orchestrator (enforced by the CLI, the poller, and the graph itself):

```yaml
repositories:
  - name: company/backend

  # Optional: poll only picks up issues carrying this label
  - name: company/frontend
    label: ai-agent
```

Paths and limits (env overrides):

| Variable | Default |
|---|---|
| `ORCHESTRATOR_REPOS_DIR` | `~/agent-repos` (base clones) |
| `ORCHESTRATOR_WORKSPACES_DIR` | `~/agent-workspaces` (per-task worktrees) |
| `ORCHESTRATOR_DATA_DIR` | `./data` (sqlite state + logs) |
| `ORCHESTRATOR_OPENCODE_TIMEOUT` | `3600` (seconds) |
| `ORCHESTRATOR_POLL_INTERVAL` | `300` (seconds) |
| `ORCHESTRATOR_OPENCODE_BIN` | `opencode` |

## Usage

```bash
# Run one issue through the full pipeline
orchestrator run company/backend#123

# Poll allowed repos for new open issues (loop, or --once)
orchestrator poll --once

# Inspect tasks
orchestrator list
orchestrator status company/backend#123

# Resume an interrupted task (state is checkpointed to SQLite)
orchestrator resume company/backend#123

# Observability
orchestrator logs company/backend#123                 # list the task's node logs
orchestrator logs company/backend#123 --node plan -f  # tail the plan log live
orchestrator watch                                    # live table of all tasks
```

## Comment triggers (`/ai-agent`)

While polling, the orchestrator also scans comments on open issues **and** open
PRs. A comment starting with the repo's `command` prefix (default `/ai-agent`)
triggers a full re-run of the task with the comment body added as extra context:

- The existing open PR is updated in place (force-push on the same branch)
- Feedback reactions on the comment: 👀 `eyes` while running, 🚀 `rocket` on
  success, -1 on failure
- Each comment is processed exactly once (tracked in the `handled_comments`
  table); a failed run is not retried automatically — post a new comment to
  retry
- PR comments only trigger for orchestrator branches (`ai/issue-*`)

## How it works

- **Isolation**: each task gets its own `git worktree` under
  `~/agent-workspaces/<owner>-<repo>-<issue>/` on branch `ai/issue-<n>`,
  created from a shared base clone in `~/agent-repos/`.
- **Plan**: `opencode run --agent plan` analyzes the issue and writes the plan
  to `.agents/plans/plan.md` (skill `plan-implementation`).
- **Implement**: `opencode run --agent build` explicitly invokes the
  `subagent-plan-execution` skill, which executes the plan by dispatching
  fresh implementer/reviewer subagents per task, then runs the quality gate.
- **Test**: a standalone opencode run executes the project's test suite; a
  non-zero exit fails the task.
- **Review**: `opencode run --agent plan` reviews issue + plan + diff and must
  emit `VERDICT: APPROVED|CHANGES_REQUIRED|NEEDS_CLARIFICATION`. Non-APPROVED
  verdicts are logged and the task still proceeds to the PR (fix loop is V2).
- **PR**: changes are committed (`Closes #n`), pushed, and a PR is created via
  `gh`. `.agents/` artifacts never enter the commit.
- **Cleanup**: after a successful PR, the task worktree and local branch are
  removed (logs and the remote branch are kept). Failed tasks keep their
  worktree for debugging.
- **Persistence**: LangGraph checkpoints + task metadata in
  `data/state/orchestrator.db` (SQLite).

## Development

```bash
uv run pytest
```

## Roadmap (V2+)

Fix loop, human-in-the-loop via GitHub comments, concurrent tasks
(`MAX_CONCURRENT_TASKS`), per-repository config (test/lint commands, model),
PostgreSQL, webhook.