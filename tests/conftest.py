"""Test fixtures: isolated dirs, allowlist config, fake agent binaries."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


_ORCHESTRATOR_ENVIRONMENT = (
    "ORCHESTRATOR_CONFIG_FILE",
    "ORCHESTRATOR_DATA_DIR",
    "ORCHESTRATOR_EXECUTOR_EXECUTION",
    "ORCHESTRATOR_EXECUTOR_REVIEW",
    "ORCHESTRATOR_EXECUTOR_TRIAGE",
    "ORCHESTRATOR_GITHUB_APP_ID",
    "ORCHESTRATOR_GITHUB_APP_INSTALLATION_ID",
    "ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE",
    "ORCHESTRATOR_GITHUB_APP_SLUG",
    "ORCHESTRATOR_LOAD_DOTENV",
    "ORCHESTRATOR_MAX_CONCURRENT",
    "ORCHESTRATOR_MODEL_EXECUTION_NAME",
    "ORCHESTRATOR_MODEL_EXECUTION_VARIANT",
    "ORCHESTRATOR_MODEL_REVIEW_NAME",
    "ORCHESTRATOR_MODEL_REVIEW_VARIANT",
    "ORCHESTRATOR_MODEL_TRIAGE_NAME",
    "ORCHESTRATOR_MODEL_TRIAGE_VARIANT",
    "ORCHESTRATOR_OPENCODE_BIN",
    "ORCHESTRATOR_OPENCODE_TIMEOUT",
    "ORCHESTRATOR_CODEX_BIN",
    "ORCHESTRATOR_CODEX_TIMEOUT",
    "ORCHESTRATOR_CLAUDE_BIN",
    "ORCHESTRATOR_CLAUDE_TIMEOUT",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "ORCHESTRATOR_POLL_INTERVAL",
    "ORCHESTRATOR_REPOS_DIR",
    "ORCHESTRATOR_SKILL_SUBAGENT_PLAN_EXECUTION",
    "ORCHESTRATOR_STALE_SECONDS",
    "ORCHESTRATOR_WORKSPACES_DIR",
)

# Isolate imports from the developer's deployment configuration. These values
# are read at module import time, before per-test monkeypatch fixtures run.
for _name in _ORCHESTRATOR_ENVIRONMENT:
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True, scope="session")
def _git_identity(tmp_path_factory):
    git_config = tmp_path_factory.mktemp("git-config") / "config"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
        subprocess.run(["git", "config", "--global", "user.email", "test@test"], check=True, capture_output=True)
        subprocess.run(["git", "config", "--global", "user.name", "test"], check=True, capture_output=True)
        yield

_TMP = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))
os.environ["ORCHESTRATOR_DATA_DIR"] = str(_TMP / "data")
os.environ["ORCHESTRATOR_REPOS_DIR"] = str(_TMP / "repos")
os.environ["ORCHESTRATOR_WORKSPACES_DIR"] = str(_TMP / "workspaces")
os.environ["ORCHESTRATOR_CONFIG_FILE"] = str(_TMP / "config.yaml")
os.environ["ORCHESTRATOR_OPENCODE_BIN"] = str(_TMP / "bin" / "fake-opencode")
os.environ["ORCHESTRATOR_CODEX_BIN"] = str(_TMP / "bin" / "fake-codex")
os.environ["ORCHESTRATOR_CLAUDE_BIN"] = str(_TMP / "bin" / "fake-claude")
os.environ["ORCHESTRATOR_LOAD_DOTENV"] = "0"

from orchestrator.main import config  # noqa: E402
from orchestrator.infra.github import auth as github_auth  # noqa: E402


@pytest.fixture(autouse=True)
def disable_github_app_auth(monkeypatch):
    """Keep unit tests independent of the developer's private key and network."""
    monkeypatch.setattr(github_auth, "installation_token", lambda: "test-installation-token")

FAKE_OPENCODE = r"""#!/usr/bin/env bash
# Fake opencode: dispatches on prompt content. Env overrides:
#   FAKE_OPCODE_FAIL=1            -> exit 1
#   FAKE_OPCODE_SLEEP=N           -> sleep N seconds first
#   FAKE_OPCODE_ARGS_FILE=<path>  -> append "agent=<agent> dir=<dir>" per run
#   FAKE_OPCODE_MODEL_FILE=<path> -> append "model=<model> variant=<variant>" per run
set -e
AGENT=""
DIR="."
MODEL=""
VARIANT=""
ARGS_FILE="${FAKE_OPCODE_ARGS_FILE:-}"
MODEL_FILE="${FAKE_OPCODE_MODEL_FILE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    -m) MODEL="$2"; shift 2 ;;
    --variant) VARIANT="$2"; shift 2 ;;
    *) PROMPT="$1"; shift ;;
  esac
done
if [[ -n "$ARGS_FILE" ]]; then
  echo "agent=$AGENT dir=$DIR" >> "$ARGS_FILE"
fi
if [[ -n "$MODEL_FILE" ]]; then
  echo "model=$MODEL variant=$VARIANT" >> "$MODEL_FILE"
fi
if [[ -n "$FAKE_OPCODE_SLEEP" ]]; then sleep "$FAKE_OPCODE_SLEEP"; fi
if [[ -n "$FAKE_OPCODE_FAIL" ]]; then echo "simulated failure" >&2; exit 1; fi
cd "$DIR"
case "$PROMPT" in
  *"enough_context"*)
    echo '{"enough_context":true,"confidence":"high","summary":"ready","missing_context":[]}'
    ;;
  *"planning the implementation"*|*"planning work item"*)
    mkdir -p .agents/plans
    cat > .agents/plans/plan.md <<'EOF'
# Plan: test feature

## Task 1: implement
**Files:** work.txt
**Dependencies:** none

Append a line to work.txt.
EOF
    echo "Plan written to .agents/plans/plan.md"
    ;;
  *"implementing GitHub issue"*|*"Implement work item"*)
    echo "implemented" >> work.txt
    echo "Implementation done."
    ;;
  *"test suite"*)
    echo "Tests pass."
    ;;
  *)
    echo "unknown prompt: $PROMPT"
    exit 1
    ;;
esac
exit 0
"""


FAKE_CODEX = r"""#!/usr/bin/env bash
# Fake codex: records exec options and dispatches on prompt content.
set -e
DIR="."
MODEL=""
REASONING=""
SANDBOX=""
APPROVAL=""
PROMPT=""
ARGS_FILE="${FAKE_CODEX_ARGS_FILE:-}"
MODEL_FILE="${FAKE_CODEX_MODEL_FILE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    exec) shift ;;
    --cd|-C) DIR="$2"; shift 2 ;;
    --sandbox) SANDBOX="$2"; shift 2 ;;
    -m|--model) MODEL="$2"; shift 2 ;;
    -c|--config)
      VALUE="$2"
      KEY="${VALUE%%=*}"
      VALUE="${VALUE#*=}"
      VALUE="${VALUE#\"}"
      VALUE="${VALUE%\"}"
      if [[ "$KEY" == "model_reasoning_effort" ]]; then REASONING="$VALUE"; fi
      if [[ "$KEY" == "approval_policy" ]]; then APPROVAL="$VALUE"; fi
      shift 2
      ;;
    *) PROMPT="$1"; shift ;;
  esac
done
if [[ -n "$ARGS_FILE" ]]; then
  echo "dir=$DIR sandbox=$SANDBOX approval=$APPROVAL" >> "$ARGS_FILE"
fi
if [[ -n "$MODEL_FILE" ]]; then
  echo "model=$MODEL reasoning=$REASONING" >> "$MODEL_FILE"
fi
if [[ -n "$FAKE_CODEX_SLEEP" ]]; then sleep "$FAKE_CODEX_SLEEP"; fi
if [[ -n "$FAKE_CODEX_FAIL" ]]; then echo "simulated failure" >&2; exit 1; fi
cd "$DIR"
case "$PROMPT" in
  *"enough_context"*)
    echo '{"enough_context":true,"confidence":"high","summary":"ready","missing_context":[]}'
    ;;
  *"ONLY valid JSON"*)
    echo '{"verdict":"comment","summary":"ok","findings":[],"checks":[]}'
    ;;
  *"planning the implementation"*|*"planning work item"*)
    mkdir -p .agents/plans
    cat > .agents/plans/plan.md <<'EOF'
# Plan: test feature

## Task 1: implement
**Files:** work.txt
**Dependencies:** none

Append a line to work.txt.
EOF
    echo "Plan written to .agents/plans/plan.md"
    ;;
  *"implementing GitHub issue"*|*"Implement work item"*)
    echo "implemented" >> work.txt
    echo "Implementation done."
    ;;
  *"test suite"*)
    echo "Tests pass."
    ;;
  *)
    echo "unknown prompt: $PROMPT"
    exit 1
    ;;
esac
exit 0
"""


FAKE_CLAUDE = r"""#!/usr/bin/env bash
# Fake Claude Code: records print-mode options and dispatches on prompt content.
# The real CLI does not accept --effort; fail if a caller tries to use it.
set -e
MODEL=""
PERMISSION=""
OUTPUT=""
AGENT=""
PROMPT=""
ARGS_FILE="${FAKE_CLAUDE_ARGS_FILE:-}"
MODEL_FILE="${FAKE_CLAUDE_MODEL_FILE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--print) shift ;;
    --output-format) OUTPUT="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --effort)
      echo "unsupported option: --effort" >&2
      exit 2
      ;;
    --permission-mode) PERMISSION="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    *) PROMPT="$1"; shift ;;
  esac
done
if [[ -n "$ARGS_FILE" ]]; then
  echo "dir=$(pwd) output=$OUTPUT permission=$PERMISSION agent=$AGENT" >> "$ARGS_FILE"
fi
if [[ -n "$MODEL_FILE" ]]; then
  echo "model=$MODEL env_effort=${CLAUDE_CODE_EFFORT_LEVEL:-}" >> "$MODEL_FILE"
fi
if [[ -n "$FAKE_CLAUDE_SLEEP" ]]; then sleep "$FAKE_CLAUDE_SLEEP"; fi
if [[ -n "$FAKE_CLAUDE_FAIL" ]]; then echo "simulated failure" >&2; exit 1; fi
case "$PROMPT" in
  *"enough_context"*)
    echo '{"enough_context":true,"confidence":"high","summary":"ready","missing_context":[]}'
    ;;
  *"ONLY valid JSON"*)
    echo '{"verdict":"comment","summary":"ok","findings":[],"checks":[]}'
    ;;
  *"planning the implementation"*|*"planning work item"*)
    mkdir -p .agents/plans
    cat > .agents/plans/plan.md <<'EOF'
# Plan: test feature

## Task 1: implement
**Files:** work.txt
**Dependencies:** none

Append a line to work.txt.
EOF
    echo "Plan written to .agents/plans/plan.md"
    ;;
  *"implementing GitHub issue"*|*"Implement work item"*)
    echo "implemented" >> work.txt
    echo "Implementation done."
    ;;
  *"test suite"*)
    echo "Tests pass."
    ;;
  *)
    echo "unknown prompt: $PROMPT"
    exit 1
    ;;
esac
exit 0
"""


@pytest.fixture(scope="session", autouse=True)
def fake_opencode_bin() -> Path:
    bin_dir = Path(os.environ["ORCHESTRATOR_OPENCODE_BIN"]).parent
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake-opencode"
    script.write_text(FAKE_OPENCODE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.fixture(scope="session", autouse=True)
def fake_codex_bin() -> Path:
    bin_dir = Path(os.environ["ORCHESTRATOR_CODEX_BIN"]).parent
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake-codex"
    script.write_text(FAKE_CODEX)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.fixture(scope="session", autouse=True)
def fake_claude_bin() -> Path:
    bin_dir = Path(os.environ["ORCHESTRATOR_CLAUDE_BIN"]).parent
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake-claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.fixture(autouse=True)
def clear_config_cache():
    config.load_repository_config.cache_clear()
    config.load_execution_model_config.cache_clear()
    config.load_review_model_config.cache_clear()
    config.load_triage_model_config.cache_clear()
    config.load_pipeline_config.cache_clear()
    yield
    config.load_repository_config.cache_clear()
    config.load_execution_model_config.cache_clear()
    config.load_review_model_config.cache_clear()
    config.load_triage_model_config.cache_clear()
    config.load_pipeline_config.cache_clear()


@pytest.fixture
def allowlist():
    path = config.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("repositories:\n  - name: company/backend\n")
    config.load_repository_config.cache_clear()
    return path


@pytest.fixture
def model_config():
    path = config.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "repositories:\n"
        "  - name: company/backend\n"
        "model:\n"
        "  execution:\n"
        "    name: verboo/deepseek-v4-flash\n"
        "    variant: high\n"
        "  review:\n"
        "    name: openai/gpt-5.6-luna\n"
        "    variant: medium\n"
    )
    config.load_repository_config.cache_clear()
    config.load_execution_model_config.cache_clear()
    config.load_review_model_config.cache_clear()
    return path


@pytest.fixture
def remote_repo(tmp_path):
    """Bare remote repo seeded with one commit on main."""
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=seed, check=True, capture_output=True)
    (seed / "work.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True)
    return str(bare)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "FAKE_OPCODE_FAIL",
        "FAKE_OPCODE_SLEEP",
        "FAKE_OPCODE_ARGS_FILE",
        "FAKE_OPCODE_MODEL_FILE",
        "FAKE_CODEX_FAIL",
        "FAKE_CODEX_SLEEP",
        "FAKE_CODEX_ARGS_FILE",
        "FAKE_CODEX_MODEL_FILE",
        "FAKE_CLAUDE_FAIL",
        "FAKE_CLAUDE_SLEEP",
        "FAKE_CLAUDE_ARGS_FILE",
        "FAKE_CLAUDE_MODEL_FILE",
        "CLAUDE_CODE_EFFORT_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)
