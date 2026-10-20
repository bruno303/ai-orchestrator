"""Tests for the packaged templates and ``ai-orchestrator init``."""

from __future__ import annotations

import stat

from orchestrator.main import config
from orchestrator.main.cli import main


def _run_init(capsys, config_dir, data_dir, env_file, *options):
    main(
        [
            "init",
            *options,
            "--config-dir",
            str(config_dir),
            "--data-dir",
            str(data_dir),
            "--env-file",
            str(env_file),
        ]
    )
    return capsys.readouterr().out


def test_init_creates_the_expected_layout(tmp_path, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"

    output = _run_init(capsys, config_dir, data_dir, env_file)

    assert (config_dir / "config.yaml").is_file()
    assert env_file.is_file()
    assert (data_dir / "state").is_dir()
    assert (data_dir / "logs").is_dir()
    assert output.splitlines() == [
        f"config dir:   {config_dir}",
        f"config file:  {config_dir / 'config.yaml'}",
        f"env file:     {env_file}",
        f"data dir:     {data_dir}",
        f"data dirs:    {data_dir / 'state'} {data_dir / 'logs'}",
        "next: ai-orchestrator doctor",
    ]


def test_second_init_skips_existing_files(tmp_path, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"
    _run_init(capsys, config_dir, data_dir, env_file)

    config_file = config_dir / "config.yaml"
    config_file.write_text("sentinel: true\n")
    env_file.write_text("SENTINEL=1\n")

    output = _run_init(capsys, config_dir, data_dir, env_file)

    assert config_file.read_text() == "sentinel: true\n"
    assert env_file.read_text() == "SENTINEL=1\n"
    assert output.count("exists, use --force") == 2


def test_force_rewrites_existing_files(tmp_path, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"
    _run_init(capsys, config_dir, data_dir, env_file)

    config_file = config_dir / "config.yaml"
    config_file.write_text("sentinel: true\n")
    env_file.write_text("SENTINEL=1\n")

    output = _run_init(capsys, config_dir, data_dir, env_file, "--force")

    assert "repositories: []" in config_file.read_text()
    assert "ORCHESTRATOR_GITHUB_APP_ID=123" in env_file.read_text()
    assert "exists, use --force" not in output


def test_dry_run_creates_nothing(tmp_path, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"

    output = _run_init(capsys, config_dir, data_dir, env_file, "--dry-run")

    assert not config_dir.exists()
    assert not data_dir.exists()
    assert str(config_dir / "config.yaml") in output
    assert str(env_file) in output


def test_generated_env_file_is_private(tmp_path, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"

    _run_init(capsys, config_dir, data_dir, env_file)

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_generated_config_is_the_packaged_template(tmp_path, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"

    _run_init(capsys, config_dir, data_dir, env_file)

    text = (config_dir / "config.yaml").read_text()
    assert "repositories: []" in text
    assert "#   - name: company/backend" in text
    assert "bruno303/" not in text


def test_generated_config_loads_through_config(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    env_file = config_dir / ".env"
    _run_init(capsys, config_dir, data_dir, env_file)
    config_file = config_dir / "config.yaml"

    monkeypatch.setenv("ORCHESTRATOR_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    config.load_pipeline_config.cache_clear()
    config.load_repository_config.cache_clear()

    pipeline = config.load_pipeline_config()

    assert pipeline.execution.executor.type == "opencode"
    assert config.load_repository_config() == {}


def test_init_defaults_follow_the_resolved_config_file(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "custom" / "my.yaml"
    env_file = tmp_path / "custom" / ".env"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ORCHESTRATOR_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("ORCHESTRATOR_ENV_FILE", str(env_file))
    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(data_dir))

    main(["init"])

    output = capsys.readouterr().out
    assert config_file.is_file()
    assert "repositories: []" in config_file.read_text()
    assert env_file.is_file()
    assert (data_dir / "state").is_dir()
    assert f"config file:  {config_file}" in output


def test_init_without_an_override_writes_the_config_home_default(tmp_path, monkeypatch, capsys):
    config_home = tmp_path / "config-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("ORCHESTRATOR_CONFIG_FILE", raising=False)
    monkeypatch.setenv("ORCHESTRATOR_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))

    main(["init"])

    assert (config_home / "ai-orchestrator" / "config.yaml").is_file()
