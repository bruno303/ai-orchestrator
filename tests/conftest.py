"""Test fixtures: isolated dirs, allowlist config, fake opencode binary."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _git_identity():
    subprocess.run(["git", "config", "--global", "user.email", "test@test"], check=True, capture_output=True)
    subprocess.run(["git", "config", "--global", "user.name", "test"], check=True, capture_output=True)

_TMP = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))
os.environ["ORCHESTRATOR_DATA_DIR"] = str(_TMP / "data")
os.environ["ORCHESTRATOR_REPOS_DIR"] = str(_TMP / "repos")
os.environ["ORCHESTRATOR_WORKSPACES_DIR"] = str(_TMP / "workspaces")
os.environ["ORCHESTRATOR_CONFIG_FILE"] = str(_TMP / "repositories.yaml")
os.environ["ORCHESTRATOR_OPENCODE_BIN"] = str(_TMP / "bin" / "fake-opencode")

from orchestrator import config  # noqa: E402

FAKE_OPENCODE = r"""#!/usr/bin/env bash
# Fake opencode: dispatches on prompt content. Env overrides:
#   FAKE_OPCODE_FAIL=1            -> exit 1
#   FAKE_OPCODE_SLEEP=N           -> sleep N seconds first
#   FAKE_OPCODE_LOOP=1            -> emit 200 identical lines, then exit 0
#   FAKE_OPCODE_LOOP_ONCE=<model> -> emit the loop only when -m matches <model>
#   FAKE_OPCODE_LOOP_PROMPT=<sub> -> only loop when the prompt contains <sub> (AND with the above)
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
SHOULD_LOOP=0
if [[ -n "$FAKE_OPCODE_LOOP" ]]; then SHOULD_LOOP=1; fi
if [[ -n "$FAKE_OPCODE_LOOP_ONCE" && "$MODEL" == "$FAKE_OPCODE_LOOP_ONCE" ]]; then SHOULD_LOOP=1; fi
if [[ -n "$FAKE_OPCODE_LOOP_PROMPT" && "$PROMPT" != *"$FAKE_OPCODE_LOOP_PROMPT"* ]]; then SHOULD_LOOP=0; fi
if [[ $SHOULD_LOOP -eq 1 ]]; then
  for i in $(seq 1 200); do echo "PERMIT ME NOW to emit exactly one invocation card"; done
  exit 0
fi
if [[ -n "$FAKE_OPCODE_SLEEP" ]]; then sleep "$FAKE_OPCODE_SLEEP"; fi
if [[ -n "$FAKE_OPCODE_FAIL" ]]; then echo "simulated failure" >&2; exit 1; fi
cd "$DIR"
case "$PROMPT" in
  *"planning the implementation"*)
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
  *"implementing GitHub issue"*)
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


@pytest.fixture(autouse=True)
def clear_config_cache():
    config.load_repository_config.cache_clear()
    config.load_model_config.cache_clear()
    config.load_fallback_enabled.cache_clear()
    yield
    config.load_repository_config.cache_clear()
    config.load_model_config.cache_clear()
    config.load_fallback_enabled.cache_clear()


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
        "  fallback_enabled: true\n"
        "  primary:\n"
        "    name: verboo/deepseek-v4-flash\n"
        "    variant: high\n"
        "  fallback:\n"
        "    name: verboo/glm-4.7-flash\n"
        "    variant: high\n"
    )
    config.load_repository_config.cache_clear()
    config.load_model_config.cache_clear()
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
        "FAKE_OPCODE_LOOP",
        "FAKE_OPCODE_LOOP_ONCE",
        "FAKE_OPCODE_LOOP_PROMPT",
        "FAKE_OPCODE_MODEL_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
