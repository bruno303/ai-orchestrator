# AI Orchestrator

Local orchestrator (LangGraph + OpenCode) that turns input events into published changes:

```
GitHub Issue → workspace (git worktree) → OpenCode plan → OpenCode build (subagent-plan-execution)
             → tests → push / PR publication → cleanup
```

## Requirements

- `uv`, `git`, `opencode` (>= 1.18), `gh` (authenticated with `repo` scope)

## Setup

```bash
uv sync
```

## Configuration

Edit `config/repositories.yaml` — only repositories listed here can trigger the
orchestrator (enforced by the CLI, the executor, and the graph itself):

```yaml
repositories:
  - name: company/backend

  # Optional: poll only picks up issues carrying this label
  - name: company/frontend
    label: ai-agent
```

Optional global model config (applies to all repositories). `primary` is used
for the first attempt of each phase. When `fallback_enabled` is true, a phase
that degenerates into a loop is retried once with `fallback`. Omitting the
section keeps opencode's default model (no `-m`/`--variant` flags):

```yaml
model:
  fallback_enabled: true
  primary:
    name: verboo/deepseek-v4-flash
    variant: high
  fallback:
    name: verboo/glm-4.7-flash
    variant: high
```

Set `fallback_enabled` to `false` (or omit it) to disable loop detection and
fallback retries entirely. The environment override is
`ORCHESTRATOR_MODEL_FALLBACK_ENABLED`.

The planning phase must produce `.agents/plans/plan.md`. The read-only planning
agent returns the plan in its response, and the orchestrator persists it. The
planner must not modify repository files. The orchestrator validates the
artifact before starting implementation.

Paths, limits, model and loop detection (env overrides):

| Variable | Default |
|---|---|
| `ORCHESTRATOR_REPOS_DIR` | `~/agent-repos` (base clones) |
| `ORCHESTRATOR_WORKSPACES_DIR` | `~/agent-workspaces` (per-task worktrees) |
| `ORCHESTRATOR_DATA_DIR` | `./data` (sqlite state + logs) |
| `ORCHESTRATOR_OPENCODE_TIMEOUT` | `3600` (seconds) |
| `ORCHESTRATOR_POLL_INTERVAL` | `300` (seconds) |
| `ORCHESTRATOR_OPENCODE_BIN` | `opencode` |
| `ORCHESTRATOR_MODEL_PRIMARY_NAME` | (none — opencode default) |
| `ORCHESTRATOR_MODEL_PRIMARY_VARIANT` | (none — opencode default) |
| `ORCHESTRATOR_MODEL_FALLBACK_NAME` | (none) |
| `ORCHESTRATOR_MODEL_FALLBACK_VARIANT` | (none) |
| `ORCHESTRATOR_MODEL_FALLBACK_ENABLED` | `false` |
| `ORCHESTRATOR_PHASE_MAX_ATTEMPTS` | `2` |
| `ORCHESTRATOR_LOOP_REPEAT_THRESHOLD` | `20` (identical lines within window) |
| `ORCHESTRATOR_LOOP_REPEAT_WINDOW` | `100` (lines) |
| `ORCHESTRATOR_LOOP_RATIO_THRESHOLD` | `0.1` (distinct-line ratio) |
| `ORCHESTRATOR_LOOP_CHECK_INTERVAL` | `25` (lines) |

## Usage

```bash
# Run one issue through the full pipeline
orchestrator run company/backend#123

# Execute issue and review workflows for allowed repos (loop, or --once)
orchestrator execute --once

# Poll open pull requests for provider-neutral AI reviews (loop, or --once)
orchestrator review --once

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

While executing, the orchestrator also scans comments on open issues **and** open
PRs. A comment starting with the repo's `command` prefix (default `/ai-agent`)
triggers a full re-run of the task with the comment body added as extra context:

- The existing open PR is updated in place (force-push on the same branch)
- Feedback reactions on the comment: 👀 `eyes` while running, 🚀 `rocket` on
  success, -1 on failure
- Each comment is processed exactly once (tracked in the `handled_comments`
  table); a failed run is not retried automatically — post a new comment to
  retry
- PR comments only trigger for orchestrator branches (`ai/issue-*`)

## Pull-request reviews

The review pipeline is provider-neutral at the same boundaries as the issue
pipeline: an input source selects pull requests, an executor inspects the
checkout, and a destination publishes the result. Configure it under the
`pipeline.review` section when it should differ from the main pipeline; it
otherwise inherits the configured providers. Run it continuously with
`make review` or once with `orchestrator review --once`. `orchestrator execute`
also runs one review pass on every poll iteration.

The GitHub input source skips pull requests carrying the processed label
(`ai-reviewed` by default). The label is added only after the review comment
has been published, so a failed review or publication is retried on the next
poll. Remove that label to request a fresh review. Reviews publish one
standard comment containing the verdict, summary, findings, and checks; valid
findings on changed diff lines may also be published as inline comments.
Inline comments are limited to lines GitHub reports as changed and do not
support arbitrary unchanged-file locations.

## How it works

The runtime is provider-neutral at its four integration boundaries:

```
InputSource -> Workflow -> Executor / WorkspaceManager -> Destination
```

The default pipeline is GitHub input polling, OpenCode, Git workspaces, and the GitHub
destination. New seeds and graph updates write only the `input`, `processing`,
`workspace`, and `output` state namespaces. This keeps LangGraph checkpoints
serializable and lets a provider retain its own metadata without adding
provider-specific fields to workflow logic. Legacy flat fields are read only
when resuming old checkpoints or compatibility callers; they are never added to
new state. Existing GitHub task IDs (`owner/repo#issue`) and SQLite task
metadata remain supported.

Pipeline providers can be selected in `config/repositories.yaml`:

```yaml
pipeline:
  input_source: {type: github_polling}
  executor: {type: opencode}
  workspace_manager: {type: git}
  destination: {type: github}
```

To add a provider, implement the relevant protocol in `providers.py`, register
its factory in the matching registry, and configure its type. Input events carry
the configured input provider identity, and provider metadata belongs in the
boundary request/result `provider_state` or the owning namespace. Do not put
service-specific values in the workflow's generic fields. The current application
workflow still assumes GitHub issue numbering, Git branches, and pull requests,
so other providers require corresponding workflow/adaptor work; the boundary is
extensible, not a claim that arbitrary sources and destinations are already
fully supported.

- **Isolation**: each task gets its own `git worktree` under
  `~/agent-workspaces/<owner>-<repo>-<issue>/` on branch `ai/issue-<n>`,
  created from a shared base clone in `~/agent-repos/`.
- **Plan**: `opencode run --agent plan` analyzes the issue and writes the plan
  to `.agents/plans/plan.md` (skill `plan-implementation`).
- **Implement**: `opencode run --agent build` explicitly invokes the
  `subagent-plan-execution` skill. That skill may dispatch fresh implementer and
  reviewer subagents per task, then runs the quality gate. These are internal
  implementation passes, not a separate orchestrator phase.
- **Model**: each phase runs opencode with the configured model — `primary`
  for the first attempt, and if the phase degenerates into a loop (repetitive
  output: identical-line flood or low distinct-line ratio), it is aborted and
  retried once with `fallback`; after `ORCHESTRATOR_PHASE_MAX_ATTEMPTS` the
  task fails. The model and variant are logged for every attempt of every
  phase (visible via `orchestrator logs <task> --node <node>`).
- **Test**: a standalone opencode run executes the project's test suite; a
  non-zero exit fails the task.
- **PR**: after the standalone test phase succeeds, changes are committed
  (`Closes #n`), pushed, and a PR is created via `gh`. `.agents/` artifacts
  never enter the commit.
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
