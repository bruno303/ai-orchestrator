# AI Orchestrator

Local orchestrator (LangGraph + OpenCode) that turns input events into published changes:

```
GitHub Issue → workspace (git worktree) → OpenCode plan → OpenCode build (subagent-plan-execution)
             → tests → push / PR publication → cleanup
```

## Requirements

- `uv`, `git`, `opencode` (>= 1.18), `gh`

## Setup

```bash
uv sync
```

## Configuration

GitHub operations authenticate with the configured GitHub App installation. The
defaults match the `bruno303-ai-agent-bot` installation used by this project;
override them for another deployment with environment variables:

| Variable | Default |
|---|---|
| `ORCHESTRATOR_GITHUB_APP_ID` | `4666139` |
| `ORCHESTRATOR_GITHUB_APP_INSTALLATION_ID` | `155320111` |
| `ORCHESTRATOR_GITHUB_APP_SLUG` | `bruno303-ai-agent-bot` |
| `ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE` | `config/key.pem` |

The private key is used only in memory to create short-lived installation tokens.
HTTPS Git operations use the same token, and commits use the App bot identity.
Keep the private key outside version control.

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
| `ORCHESTRATOR_DATA_DIR` | `./data` (logs and poll lock) |
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

# Reset local workspace state and make a developed issue eligible again
orchestrator reset company/backend#123

# Observability
orchestrator logs company/backend#123                 # list the task's node logs
orchestrator logs company/backend#123 --node plan     # read a node log
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

## Architecture

The package layout keeps policy and external adapters separate. `main` is the
only composition root: it loads deployment configuration, selects providers,
and wires concrete adapters into application services.

```
src/orchestrator/
├── domain/        # business values and invariants; Python standard library only
├── application/   # use cases, workflows, and provider protocols
├── infra/         # GitHub, git, filesystem, LangGraph, and OpenCode adapters
└── main/          # config, provider registries, composition, and CLI
```

Dependencies always point inward:

```
main ──────────────> application ───> domain
  │                       ▲
  └──> infra ─────────────┘
```

Place a new business rule or value object in `domain`; add a use case or a
provider protocol in `application`; implement external I/O in `infra`; and
register/wire a concrete provider in `main/providers.py` and
`main/composition.py`. Infrastructure must not import `main`, and no flat
compatibility modules are retained at the package root.

Provider adapters translate external events into neutral domain values.
Workflow engines decide which step runs next; reusable runtime services perform
the side effects. Execution and review remain separate:

```
GitHub Issue/Comment
    -> GitHub Work Input + Feedback
    -> WorkItem(id: str, context: Context) / InputEvent
    -> Generic execution workflow/runtime
    -> Destination.publish(ChangeRequest + Context)
    -> GitHub Change Destination
    -> GitHub PR

GitHub PR
    -> GitHub Review Input
    -> ReviewTarget(id: str, context: Context)
    -> Generic review workflow/runtime
    -> ReviewDestination.publish(ReviewTarget, ReviewOutcome)
    -> GitHub review + processed marker
```

Runtime operations accept typed request objects rather than LangGraph state, so
the same issue and review steps can later be called by an HTTP API, n8n, or
another workflow engine without duplicating OpenCode, git, or provider logic.
`GitWorkspaceManager` is provider-neutral: adapters provide explicit clone/fetch
URLs, refs, revisions, checkout mode, and workspace paths; it performs only git
clone, fetch, worktree, and cleanup operations.
LangGraph routes a single in-memory execution. The review workflow remains
independently invokable and has its own GitHub `ai-reviewed` marker.

Every task or review target has exactly one mandatory, non-empty, opaque string
ID. Providers use their stable identifier when one exists (`owner/repo#123`,
`ABC-42`, or `MR-abc`). An input boundary calls `ensure_task_id` once to create
a UUID when its source has no identifier. IDs are never converted to integers,
and there is no separate `source_item_id`.

Cross-step integration state is a JSON-serializable `Context` whose top-level
keys are provider-owned namespaces. For example, GitHub source metadata belongs
under `github`, checkout metadata under `git`, and executor session data under
`opencode`. Updating one namespace preserves all unrelated namespaces. Generic
application and runtime code may pass and merge Context, but it must never read
provider-specific namespaces or keys. Providers may read and update their own
namespace. Context is data-only and safe to pass to future HTTP or n8n boundaries.

Provider-specific logging enrichment is supplied by an optional
`ContextPresenter`. The generic application logs the presenter's generic
key/value result and does not know how provider metadata was extracted.

Publication is provider-neutral: execution destinations receive a
`ChangeRequest` and return `PublishedChange`; review destinations receive a
`ReviewTarget` plus typed `ReviewOutcome` and return `PublishedReview`. A GitHub
destination interprets `context.github` to implement issue-closing text, PR
reuse, inline review validation, and processed labels. None of that behavior is
owned by the generic runtime.

The default pipeline is GitHub input polling, OpenCode, Git workspaces, and the GitHub
destination. New seeds and graph updates use only the `input`, `processing`,
`workspace`, and `output` state namespaces. GitHub supplies durable execution
state: successful publication adds `ai-developed` to the source issue; an
unlabeled issue is retried from the beginning after an interruption.

Pipeline providers can be selected in `config/repositories.yaml`:

```yaml
pipeline:
  input_source: {type: github_polling}
  executor: {type: opencode}
  workspace_manager: {type: git}
  destination: {type: github}
```

To add a provider, implement the relevant protocol in `application/ports`, add
its concrete adapter under `infra`, register its factory in `main/providers.py`,
and configure its type. Input events carry
the configured input provider identity, and provider metadata belongs in its
Context namespace. Do not put service-specific values in generic fields.

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
- **Execution state**: GitHub is the durable source of truth. A source issue
  receives `ai-developed` only after its PR is published; interrupted work is
  retried from the beginning while it remains unlabeled.

## Development

```bash
uv run pytest
```

## Roadmap (V2+)

Fix loop, human-in-the-loop via GitHub comments, concurrent tasks
(`MAX_CONCURRENT_TASKS`), per-repository config (test/lint commands, model),
PostgreSQL, webhook.
