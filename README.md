# AI Orchestrator

Local orchestrator (LangGraph + OpenCode, Codex, or Claude Code) that turns input events into published changes:

```
GitHub Issue → triage (`make triage`) → workspace (git worktree) → agent plan → agent build with tests and quality checks
             (subagent-plan-execution) → push / PR publication → cleanup
```

## Requirements

- `uv`, `git`, `gh`, and the CLI for the selected agent provider (`opencode` >= 1.18, `codex`, or `claude`)

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
| `ORCHESTRATOR_GITHUB_APP_ID` | `123` |
| `ORCHESTRATOR_GITHUB_APP_INSTALLATION_ID` | `123` |
| `ORCHESTRATOR_GITHUB_APP_SLUG` | `bruno303-ai-agent-bot` |
| `ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE` | `config/key.pem` |

The private key is used only in memory to create short-lived installation tokens.
HTTPS Git operations use the same token, and commits use the App bot identity.
Keep the private key outside version control.

Copy `.env.example` to `.env` to override these and the runtime defaults below.
The application loads `.env` automatically; exported shell variables take precedence.
The test suite disables `.env` loading to remain isolated from local deployment settings.

Edit `config/config.yaml` — only repositories listed here can trigger the
orchestrator (enforced by the CLI, the executor, and the graph itself):

```yaml
repositories:
  - name: company/backend
  - name: company/frontend
```

Workflow labels are configured per pipeline stage, not per repository. The
default hand-off contract is `ai-agent` for execution readiness, `ai-triage`
for blocked triage, `ai-developed` after PR publication, and `ai-reviewed`
after review publication. A legacy repository entry with `label: ai-agent` is
accepted as a no-op; other repository label values are rejected.

Configure agent models independently for issue execution, pull-request review,
and issue triage. Omitting a section keeps the selected provider's default model:

```yaml
model:
  execution:
    name: verboo/deepseek-v4-flash
    variant: high
  review:
    name: verboo/deepseek-v4-flash
    variant: high
  triage:
    name: verboo/deepseek-v4-flash
    variant: high
```

Set `ORCHESTRATOR_MODEL_EXECUTION_NAME` / `ORCHESTRATOR_MODEL_EXECUTION_VARIANT`
`ORCHESTRATOR_MODEL_REVIEW_NAME` / `ORCHESTRATOR_MODEL_REVIEW_VARIANT`, or
`ORCHESTRATOR_MODEL_TRIAGE_NAME` / `ORCHESTRATOR_MODEL_TRIAGE_VARIANT` to
override the corresponding `config.yaml` values through the environment.

Select the executor independently for issue execution, pull-request review,
and triage with `ORCHESTRATOR_EXECUTOR_EXECUTION`,
`ORCHESTRATOR_EXECUTOR_REVIEW`, and `ORCHESTRATOR_EXECUTOR_TRIAGE`.
Each accepts `opencode`, `codex`, or `claude` and overrides only the matching
`pipeline.*.executor.type` value; any executor options in `config.yaml` remain
in effect. For example, use `ORCHESTRATOR_EXECUTOR_EXECUTION=codex` while
keeping the review executor configured in YAML.

OpenCode receives `name` and `variant` as its model flags. Codex receives
`name` as `codex exec -m` and maps `variant` to the Codex
`model_reasoning_effort` setting. Claude Code receives `name` as `--model` and
sets `CLAUDE_CODE_EFFORT_LEVEL` for the configured `variant`.

When selecting Claude, configure Claude-compatible model names for each
workflow (for example, `sonnet` or `opus`). Claude runs each phase with its
prompt; the generic `plan`/`build` agent name is not passed as a Claude custom
agent flag. It uses Claude's default permission policy for issue execution
unless `pipeline.execution.executor.permission_mode` is set; PR reviews default
to Claude's read-only `plan` mode. Configure a non-default execution policy
deliberately—for example `acceptEdits`—and use `bypassPermissions` only in an
isolated environment. Claude authenticates through its own CLI configuration;
no Claude credentials belong in this project's `.env` file.

The planning phase must produce `.agents/plans/plan.md`. The read-only planning
agent returns the plan in its response, and the orchestrator persists it. The
planner must not modify repository files. The orchestrator validates the
artifact before starting implementation.

Paths, limits, model and loop detection (env overrides):

| Variable | Default |
|---|---|
| `ORCHESTRATOR_REPOS_DIR` | `~/agent-repos` (base clones) |
| `ORCHESTRATOR_WORKSPACES_DIR` | `~/agent-workspaces` (per-task worktrees) |
| `ORCHESTRATOR_DATA_DIR` | `./data` (logs and poll locks) |
| `ORCHESTRATOR_OPENCODE_TIMEOUT` | `3600` (seconds) |
| `ORCHESTRATOR_POLL_INTERVAL` | `300` (seconds) |
| `ORCHESTRATOR_OPENCODE_BIN` | `opencode` |
| `ORCHESTRATOR_CODEX_BIN` | `codex` |
| `ORCHESTRATOR_CODEX_TIMEOUT` | `3600` (seconds) |
| `ORCHESTRATOR_CLAUDE_BIN` | `claude` |
| `ORCHESTRATOR_CLAUDE_TIMEOUT` | `3600` (seconds) |
| `ORCHESTRATOR_EXECUTOR_EXECUTION` | `pipeline.execution.executor.type` |
| `ORCHESTRATOR_EXECUTOR_REVIEW` | `pipeline.review.executor.type` |
| `ORCHESTRATOR_EXECUTOR_TRIAGE` | `pipeline.triage.executor.type` |
| `ORCHESTRATOR_MODEL_EXECUTION_NAME` | `model.execution.name` |
| `ORCHESTRATOR_MODEL_EXECUTION_VARIANT` | `model.execution.variant` |
| `ORCHESTRATOR_MODEL_REVIEW_NAME` | `model.review.name` |
| `ORCHESTRATOR_MODEL_REVIEW_VARIANT` | `model.review.variant` |
| `ORCHESTRATOR_MODEL_TRIAGE_NAME` | `model.triage.name` |
| `ORCHESTRATOR_MODEL_TRIAGE_VARIANT` | `model.triage.variant` |

## Usage

```bash
# Run one issue through the full pipeline
orchestrator run company/backend#123

# Triage open issues (loop, or --once)
orchestrator triage --once

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

The GitHub input source skips pull requests carrying the stage's suppressed
labels (`ai-reviewed` by default). The completion label is added only after
the review comment has been published, so a failed review or publication is retried on the next
poll. Remove that label to request a fresh review. Reviews publish one
standard comment containing the verdict, summary, findings, and checks; valid
findings on changed diff lines may also be published as inline comments.
Inline comments are limited to lines GitHub reports as changed and do not
support arbitrary unchanged-file locations.

## Issue triage

The triage workflow examines all open issues in configured repositories that do
not have `ai-agent`, `ai-triage`, or `ai-developed`. It asks the configured agent for JSON containing
`enough_context`, a `confidence` (`low`, `medium`, or `high`), a summary, and any
missing context. Only `enough_context: true` with `confidence: high` adds
`ai-agent` and removes `ai-triage`.

Other valid assessments receive a comment with the conclusion and missing
context, followed by `ai-triage`. When the author adds the missing details,
remove `ai-triage` to make the issue eligible for another triage pass. Agent or
malformed-response failures add neither label and are retried on the next poll.

## How it works

## Architecture

The package layout keeps policy and external adapters separate. `main` is the
only composition root: it loads deployment configuration, selects providers,
and wires concrete adapters into application services.

```
src/orchestrator/
├── domain/        # business values and invariants; Python standard library only
├── application/   # use cases, workflows, and provider protocols
├── infra/         # GitHub, git, filesystem, LangGraph, and agent adapters
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
another workflow engine without duplicating agent, git, or provider logic.
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
the selected provider namespace. Updating one namespace preserves all unrelated namespaces. Generic
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

The default pipeline is GitHub input polling, OpenCode, Git workspaces, and the
GitHub destination. Triage OpenCode runs receive a fixed read-only permission
configuration even though the wrapper uses `--auto`; shell, edits, subagents,
external directories, and network tools remain denied. Set an executor to
`type: codex` or `type: claude` to use that CLI provider instead.
New seeds and graph updates use only the `input`, `processing`,
`workspace`, and `output` state namespaces. GitHub supplies durable execution
state: polling selects open, unassigned issues matching the execution stage
contract, assigns the authenticated GitHub user
before execution, and successful publication adds `ai-developed` to the source
issue. Assignment failures are logged and skipped so polling can continue;
issues assigned before an interruption are no longer selected as new work.

Pipeline providers can be selected in `config/config.yaml`:

```yaml
pipeline:
  execution:
    input_source: {type: github_polling, auth: user}
    executor: {type: opencode} # or {type: codex} or {type: claude}
    workspace_manager: {type: git, auth: user}
    destination: {type: github, auth: user}
  review:
    input_source: {type: github_polling, auth: bot}
    executor: {type: opencode} # or {type: codex} or {type: claude}
    workspace_manager: {type: git, auth: bot}
    destination: {type: github, auth: bot}
```

The execution and review sections are independent; omitted provider settings use
their workflow's built-in defaults and never inherit values from the other section.

Each GitHub-facing provider (`github_polling` and `github`) and the `git`
workspace provider accepts `auth: bot | user`; omitted `auth` defaults to `bot`.
`bot` uses the configured GitHub App installation token and its commit identity.
`user` leaves authentication and Git author settings to the account configured on
the host. The recommended layout above runs issue execution as the local user and
reviews as the bot. Before using `user`, run `gh auth login`, `gh auth setup-git`,
and configure `git config user.name` plus `git config user.email`.

To add a provider, implement the relevant protocol in `application/ports`, add
its concrete adapter under `infra`, register its factory in `main/providers.py`,
and configure its type. Input events carry
the configured input provider identity, and provider metadata belongs in its
Context namespace. Do not put service-specific values in generic fields.

- **Isolation**: each task gets its own `git worktree` under
  `~/agent-workspaces/<owner>-<repo>-<issue>/` on branch `ai/issue-<n>`,
  created from a shared base clone in `~/agent-repos/`.
- **Assignment**: polling selects only unassigned issues matching the
  execution stage's labels and assigns the authenticated GitHub user
  before starting work. A failed assignment is logged and the issue is skipped
  for that poll.
- **Plan**: the selected provider analyzes the issue and writes the plan
  to `.agents/plans/plan.md` (skill `plan-implementation`).
- **Implement**: the selected provider explicitly uses the
  `plan-implementation` skill to execute `.agents/plans/plan.md` and modify the
  workspace. The agent must run the repository's appropriate tests and quality
  checks during implementation, fix failures, and must not stop after creating,
  revising, or saving a plan.
- **Model**: issue phases use the configured `execution` model and pull-request
  reviews use the configured `review` model. The model and variant are logged for each phase (visible via
  `orchestrator logs <task> --node <node>`).
- **PR**: after implementation and its validation succeed, changes are
  committed (`Closes #n`), pushed, and a PR is created via `gh`. `.agents/` artifacts
  never enter the commit.
- **Cleanup**: after a successful PR, the task worktree and local branch are
  removed (logs and the remote branch are kept). Failed tasks keep their
  worktree for debugging until a rerun starts; reruns discard and recreate the
  task workspace from the base branch.
- **Execution state**: GitHub is the durable source of truth. A source issue
  is assigned before work starts and receives `ai-developed` only after its PR
  is published. Use a comment command or direct `run` to rerun interrupted
  work.

## Development

```bash
uv run pytest
```

## Roadmap (V2+)

Fix loop, human-in-the-loop via GitHub comments, concurrent tasks
(`MAX_CONCURRENT_TASKS`), per-repository config (test/lint commands, model),
PostgreSQL, webhook.
