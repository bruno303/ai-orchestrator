"""GitHub App authentication for API and HTTPS Git operations."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import jwt


APP_ID = os.environ.get("ORCHESTRATOR_GITHUB_APP_ID", "4666139")
INSTALLATION_ID = os.environ.get("ORCHESTRATOR_GITHUB_APP_INSTALLATION_ID", "155320111")
APP_SLUG = os.environ.get("ORCHESTRATOR_GITHUB_APP_SLUG", "bruno303-ai-agent-bot")
PRIVATE_KEY_FILE = Path(os.environ.get(
    "ORCHESTRATOR_GITHUB_APP_PRIVATE_KEY_FILE",
    Path(__file__).resolve().parents[4] / "config" / "key.pem",
))
BOT_LOGIN = f"app/{APP_SLUG}"
BOT_NAME = f"{APP_SLUG}[bot]"
BOT_EMAIL = f"{APP_ID}+{APP_SLUG}[bot]@users.noreply.github.com"

_cached_token: str | None = None
_cached_expires_at = 0.0


def installation_token() -> str:
    """Return a cached installation token, refreshing it before expiration."""
    global _cached_token, _cached_expires_at
    if _cached_token and _cached_expires_at - time.time() > 60:
        return _cached_token

    now = int(time.time())
    with PRIVATE_KEY_FILE.open("rb") as key_file:
        assertion = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": APP_ID},
            key_file.read(),
            algorithm="RS256",
        )
    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {assertion}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-orchestrator",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload: dict[str, Any] = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise RuntimeError(f"could not obtain GitHub App installation token: {exc}") from exc

    token = payload.get("token")
    expires_at = payload.get("expires_at")
    if not token or not expires_at:
        raise RuntimeError("GitHub App token response was missing token or expiration")
    _cached_token = str(token)
    _cached_expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp()
    return _cached_token


def gh_environment() -> dict[str, str]:
    """Return an environment that makes gh authenticate as the installation."""
    environment = os.environ.copy()
    environment["GH_TOKEN"] = installation_token()
    return environment


def git_environment() -> dict[str, str]:
    """Return Git HTTPS credentials and bot author/committer identity."""
    token = installation_token()
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    environment = os.environ.copy()
    environment.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
        "GIT_AUTHOR_NAME": BOT_NAME,
        "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": BOT_NAME,
        "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    })
    return environment
