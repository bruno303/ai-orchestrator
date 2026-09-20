from pathlib import Path

from orchestrator.infra.sandbox.settings import load_provider_sandbox_settings


def test_provider_images_default_per_provider(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("sandbox: {}\n")

    assert load_provider_sandbox_settings("opencode", config).image.endswith("-opencode:latest")
    assert load_provider_sandbox_settings("codex", config).image.endswith("-codex:latest")
    assert load_provider_sandbox_settings("claude", config).image.endswith("-claude:latest")


def test_provider_settings_parse_limits_and_image_override(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "sandbox:\n"
        "  images:\n"
        "    codex: custom/codex:test\n"
        "  cpus: 2\n"
        "  memory: 3g\n"
        "  pids_limit: 200\n"
        "  docker_socket: false\n"
    )

    settings = load_provider_sandbox_settings("codex", config)
    assert settings.image == "custom/codex:test"
    assert settings.cpus == "2"
    assert settings.memory == "3g"
    assert settings.pids_limit == 200
    assert settings.docker_socket is None


def test_provider_mounts_do_not_leak_between_providers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: home)
    config = tmp_path / "config.yaml"
    config.write_text("sandbox: {}\n")

    opencode = load_provider_sandbox_settings("opencode", config)
    codex = load_provider_sandbox_settings("codex", config)
    claude = load_provider_sandbox_settings("claude", config)

    assert all("opencode" in mount.target or ".agents/skills" in mount.target for mount in opencode.mounts)
    assert [mount.target for mount in codex.mounts] == ["/home/agent/.codex"]
    assert {mount.target for mount in claude.mounts} == {"/home/agent/.claude", "/home/agent/.claude.json"}
    assert all(mount.read_only for settings in (opencode, codex, claude) for mount in settings.mounts)
    assert opencode.tmpfs_mounts == ("/home/agent/.local/share/opencode/log",)
    assert opencode.writable_copies == ((
        "/home/agent/.local/share/opencode",
        "/tmp/opencode/data/opencode",
    ),)
    assert codex.tmpfs_mounts == ()
    assert claude.tmpfs_mounts == ()
    assert codex.writable_copies == ()
    assert claude.writable_copies == ()
