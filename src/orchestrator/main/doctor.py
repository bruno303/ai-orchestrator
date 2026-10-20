"""Host diagnostics behind ``ai-orchestrator doctor``.

Every check reports exactly one ``ok``, ``warn``, or ``fail`` line.  The checks
never raise: subprocess and filesystem errors are turned into check results so
the command can always tell the user what is wrong with this host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.infra.claude.executor import _find_claude
from orchestrator.infra.codex.executor import _find_codex
from orchestrator.infra.opencode.executor import _find_opencode
from orchestrator.main import config

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

TIMEOUT_SECONDS = 10

EXECUTOR_ENV_VARS = {
    "opencode": "ORCHESTRATOR_OPENCODE_BIN",
    "codex": "ORCHESTRATOR_CODEX_BIN",
    "claude": "ORCHESTRATOR_CLAUDE_BIN",
}

# Reuse the adapters' own resolvers so ``doctor`` and the runtime agree on
# where a binary lives, including the ``~/.<provider>/bin`` fallbacks.
EXECUTOR_FINDERS = {
    "opencode": _find_opencode,
    "codex": _find_codex,
    "claude": _find_claude,
}

STAGES = ("execution", "review", "triage")
# Only GitHub/Git providers consume ``auth``; executors do not.
AUTH_PROVIDERS = ("input_source", "workspace_manager", "destination")


def _one_line(text: str) -> str:
    """Collapse a detail onto a single line.

    Parser errors (PyYAML in particular) are multi-line, and ``doctor``
    promises exactly one line per check.  Runs of whitespace are collapsed
    instead of truncated so the message and its source location stay visible.
    """
    return " ".join(str(text).split())


@dataclass(frozen=True)
class Check:
    """One diagnostic line: a stable name, a status, and a human detail."""

    name: str
    status: str
    detail: str

    def __post_init__(self) -> None:
        # Normalizing here guarantees every check detail is single-line, no
        # matter which check produced it.
        object.__setattr__(self, "detail", _one_line(self.detail))


def _guard(name: str, check: Callable[[], Check]) -> Check:
    """Run one check, converting unexpected errors into a check line."""
    try:
        return check()
    except Exception as exc:  # diagnostics must never crash the command
        return Check(name, STATUS_FAIL, f"unexpected error: {exc}")


def _run(command: list[str], *, timeout: float = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run a short-lived diagnostic command with a bounded timeout."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _output(result: subprocess.CompletedProcess) -> str:
    """Return the most useful single line of captured command output."""
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    if not text:
        return f"exit status {result.returncode}"
    return text.splitlines()[0].strip()


def _pipeline_config() -> config.PipelineConfig | None:
    """Load the pipeline config, returning ``None`` when it cannot be read."""
    try:
        return config.load_pipeline_config()
    except Exception:
        return None


def _auth_providers(stage) -> list[config.ProviderConfig]:
    """Return the auth-aware providers configured for one stage."""
    providers = []
    for name in AUTH_PROVIDERS:
        provider = getattr(stage, name, None)
        if provider is not None:
            providers.append(provider)
    return providers


def _auth_modes(pipeline: config.PipelineConfig | None) -> tuple[bool, bool]:
    """Return ``(uses_user_auth, uses_bot_auth)`` across every stage."""
    if pipeline is None:
        # Bot is the default, so an unreadable pipeline config still implies
        # the default bot identity.
        return False, True
    uses_user = uses_bot = False
    for stage_name in STAGES:
        stage = getattr(pipeline, stage_name, None)
        if stage is None:
            continue
        for provider in _auth_providers(stage):
            if provider.options.get("auth", "bot") == "user":
                uses_user = True
            else:
                uses_bot = True
    return uses_user, uses_bot


def _check_python() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        return Check("python", STATUS_OK, version)
    return Check("python", STATUS_FAIL, f"{version} (requires 3.11 or newer)")


def _check_config_file() -> Check:
    if not config.CONFIG_FILE.exists():
        return Check(
            "config file",
            STATUS_FAIL,
            f"no config file at {config.CONFIG_FILE}; run `ai-orchestrator init`",
        )
    try:
        config.load_pipeline_config()
        config.load_repository_config()
    except Exception as exc:
        return Check("config file", STATUS_FAIL, str(exc))
    return Check("config file", STATUS_OK, str(config.CONFIG_FILE))


def _check_allowlist() -> Check:
    if not config.CONFIG_FILE.exists():
        return Check(
            "repository allowlist",
            STATUS_FAIL,
            f"no config file at {config.CONFIG_FILE}; run `ai-orchestrator init` "
            "then list your repositories",
        )
    try:
        repositories = config.allowed_repositories()
    except Exception as exc:
        return Check("repository allowlist", STATUS_FAIL, str(exc))
    if not repositories:
        return Check(
            "repository allowlist",
            STATUS_FAIL,
            "no repositories allowed; run `ai-orchestrator init` then list your repositories",
        )
    return Check("repository allowlist", STATUS_OK, ", ".join(repositories))


def _writable(directory: Path) -> None:
    """Prove a directory is creatable and writable, raising on failure."""
    directory.mkdir(parents=True, exist_ok=True)
    handle, probe = tempfile.mkstemp(dir=directory, prefix=".doctor-")
    os.close(handle)
    Path(probe).unlink()


def _check_data_directories() -> Check:
    directories = (config.DATA_DIR, config.LOGS_DIR, config.STATE_DIR)
    for directory in directories:
        try:
            _writable(directory)
        except OSError as exc:
            return Check("data directories", STATUS_FAIL, f"{directory}: {exc}")
    return Check("data directories", STATUS_OK, ", ".join(str(path) for path in directories))


def _check_binary(name: str, binary: str) -> Check:
    """Check that ``binary`` is on PATH and answers ``--version``."""
    path = shutil.which(binary)
    if path is None:
        return Check(name, STATUS_FAIL, f"{binary} not found on PATH")
    try:
        result = _run([path, "--version"])
    except subprocess.TimeoutExpired:
        return Check(name, STATUS_WARN, f"{binary} --version timed out after {TIMEOUT_SECONDS}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(name, STATUS_WARN, f"{binary} --version failed: {exc}")
    if result.returncode != 0:
        return Check(name, STATUS_WARN, f"{binary} --version failed: {_output(result)}")
    return Check(name, STATUS_OK, _output(result))


def _check_gh_auth(uses_user: bool) -> Check:
    name = "gh auth"
    if not uses_user:
        return Check(name, STATUS_OK, "skipped: no stage uses user auth")
    path = shutil.which("gh")
    binary = path or "gh"
    try:
        result = _run([binary, "auth", "status"])
    except subprocess.TimeoutExpired:
        return Check(name, STATUS_FAIL, f"gh auth status timed out after {TIMEOUT_SECONDS}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(name, STATUS_FAIL, f"gh auth status failed: {exc}")
    if result.returncode != 0:
        return Check(name, STATUS_FAIL, f"gh auth status failed: {_output(result)}")
    return Check(name, STATUS_OK, "authenticated")


def _check_private_key(uses_bot: bool) -> Check:
    name = "github app key"
    if not uses_bot:
        return Check(name, STATUS_OK, "skipped: no stage uses bot auth")
    from orchestrator.infra.github import auth as github_auth

    key_path = Path(github_auth.PRIVATE_KEY_FILE)
    try:
        with key_path.open("rb") as key_file:
            key_file.read(1)
    except FileNotFoundError:
        return Check(name, STATUS_FAIL, f"private key not found: {key_path}")
    except OSError as exc:
        return Check(name, STATUS_FAIL, f"private key not readable: {key_path} ({exc})")
    return Check(name, STATUS_OK, str(key_path))


def _resolve_executor(provider: str) -> str | None:
    """Resolve an executor binary exactly like the adapter that runs it."""
    variable = EXECUTOR_ENV_VARS.get(provider)
    finder = EXECUTOR_FINDERS.get(provider)
    if variable is None or finder is None:
        return shutil.which(provider)
    override = os.environ.get(variable)
    if override:
        return override
    found = finder()
    # Adapters return the bare provider name as a last resort and let the OS
    # resolve it; a diagnostic can only report a binary that actually exists.
    return found if Path(found).is_file() else None


def _check_executor(stage: str, provider: str) -> Check:
    name = f"executor ({stage})"
    binary = _resolve_executor(provider)
    if not binary:
        return Check(name, STATUS_WARN, f"{provider} not found on PATH or in a known install directory")
    try:
        result = _run([binary, "--version"])
    except subprocess.TimeoutExpired:
        return Check(name, STATUS_WARN, f"{binary} --version timed out after {TIMEOUT_SECONDS}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(name, STATUS_WARN, f"{binary} --version failed: {exc}")
    if result.returncode != 0:
        return Check(name, STATUS_WARN, f"{binary} --version failed: {_output(result)}")
    return Check(name, STATUS_OK, _output(result))


def _executor_checks(pipeline: config.PipelineConfig | None) -> list[Check]:
    checks: list[Check] = []
    for stage_name in STAGES:
        stage = getattr(pipeline, stage_name, None) if pipeline is not None else None
        provider = stage.executor.type if stage is not None else "opencode"
        checks.append(
            _guard(
                f"executor ({stage_name})",
                lambda s=stage_name, p=provider: _check_executor(s, p),
            )
        )
    return checks


def run_checks() -> list[Check]:
    """Return every host diagnostic check in stable order."""
    pipeline = _pipeline_config()
    uses_user, uses_bot = _auth_modes(pipeline)
    checks = [
        _guard("python", _check_python),
        _guard("config file", _check_config_file),
        _guard("repository allowlist", _check_allowlist),
        _guard("data directories", _check_data_directories),
        _guard("git", lambda: _check_binary("git", "git")),
        _guard("gh", lambda: _check_binary("gh", "gh")),
        _guard("gh auth", lambda: _check_gh_auth(uses_user)),
        _guard("github app key", lambda: _check_private_key(uses_bot)),
    ]
    checks.extend(_executor_checks(pipeline))
    return checks
