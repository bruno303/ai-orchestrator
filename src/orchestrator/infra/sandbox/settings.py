"""Provider-specific policy layered on top of the generic sandbox runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .runner import SandboxMount


LEGACY_DEFAULT_IMAGE = "bruno303/ai-orchestrator-agent:latest"
DEFAULT_PROVIDER_IMAGES = {
    "opencode": "bruno303/ai-orchestrator-agent-opencode:latest",
    "codex": "bruno303/ai-orchestrator-agent-codex:latest",
    "claude": "bruno303/ai-orchestrator-agent-claude:latest",
}


@dataclass(frozen=True)
class ProviderSandboxSettings:
    image: str
    cpus: str
    memory: str
    pids_limit: int
    docker_socket: str | None
    mounts: tuple[SandboxMount, ...]
    tmpfs_mounts: tuple[str, ...]
    writable_copies: tuple[tuple[str, str], ...]


def _provider_mounts(provider: str) -> tuple[SandboxMount, ...]:
    """Expose only state belonging to the selected provider, read-only."""
    home = Path.home()
    specs: dict[str, tuple[tuple[str, str], ...]] = {
        "opencode": (
            (".config/opencode", "/home/agent/.config/opencode"),
            (".local/share/opencode", "/home/agent/.local/share/opencode"),
            (".agents/skills", "/home/agent/.agents/skills"),
        ),
        "codex": ((".codex", "/home/agent/.codex"),),
        "claude": (
            (".claude", "/home/agent/.claude"),
            (".claude.json", "/home/agent/.claude.json"),
        ),
    }
    mounts: list[SandboxMount] = []
    for source_suffix, target in specs.get(provider, ()):
        source = home / source_suffix
        if source.exists():
            mounts.append(SandboxMount(source.resolve(), target, True))
    return tuple(mounts)


def _provider_tmpfs_mounts(provider: str) -> tuple[str, ...]:
    """Return writable overlays for provider state that must not persist."""
    if provider == "opencode":
        return ("/home/agent/.local/share/opencode/log",)
    return ()


def _provider_writable_copies(provider: str) -> tuple[tuple[str, str], ...]:
    """Copy provider credentials/state into an ephemeral writable directory."""
    if provider == "opencode":
        return ((
            "/home/agent/.local/share/opencode",
            "/tmp/opencode/data/opencode",
        ),)
    return ()


def _load_sandbox_mapping(config_file: Path) -> dict[str, Any]:
    if not config_file.exists():
        return {}
    with config_file.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("configuration must be a mapping")
    raw = data.get("sandbox") or {}
    if not isinstance(raw, dict):
        raise ValueError("sandbox must be a mapping")
    return raw


def load_provider_sandbox_settings(provider: str, config_file: Path) -> ProviderSandboxSettings:
    """Load provider image, host-Docker access, limits and read-only state mounts."""
    raw = _load_sandbox_mapping(config_file)

    images = raw.get("images") or {}
    if not isinstance(images, dict):
        raise ValueError("sandbox.images must be a mapping")
    configured_image = images.get(provider)
    if configured_image is None:
        legacy_image = raw.get("image")
        if legacy_image and legacy_image != LEGACY_DEFAULT_IMAGE:
            configured_image = legacy_image
        else:
            configured_image = DEFAULT_PROVIDER_IMAGES.get(provider, LEGACY_DEFAULT_IMAGE)
    if not isinstance(configured_image, str) or not configured_image.strip():
        raise ValueError(f"sandbox image for {provider} must be a non-empty string")

    cpus = raw.get("cpus", 4)
    if not isinstance(cpus, (str, int, float)) or isinstance(cpus, bool) or str(cpus).strip() == "":
        raise ValueError("sandbox.cpus must be a number or non-empty string")

    memory = raw.get("memory", "4g")
    if not isinstance(memory, str) or not memory.strip():
        raise ValueError("sandbox.memory must be a non-empty string")

    pids_limit = raw.get("pids_limit", 512)
    if not isinstance(pids_limit, int) or isinstance(pids_limit, bool) or pids_limit <= 0:
        raise ValueError("sandbox.pids_limit must be a positive integer")

    docker_socket = raw.get("docker_socket", "/var/run/docker.sock")
    if docker_socket is False or docker_socket is None:
        docker_socket = None
    elif not isinstance(docker_socket, str) or not docker_socket.strip():
        raise ValueError("sandbox.docker_socket must be a non-empty path, false, or null")

    return ProviderSandboxSettings(
        image=configured_image.strip(),
        cpus=str(cpus),
        memory=memory.strip(),
        pids_limit=pids_limit,
        docker_socket=docker_socket,
        mounts=_provider_mounts(provider),
        tmpfs_mounts=_provider_tmpfs_mounts(provider),
        writable_copies=_provider_writable_copies(provider),
    )
