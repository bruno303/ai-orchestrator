"""Create a fresh configuration and data layout from packaged templates."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

CONFIG_TEMPLATE = "config.example.yaml"
ENV_TEMPLATE = "env.example"
CONFIG_FILE_NAME = "config.yaml"
DATA_SUBDIRECTORIES = ("state", "logs")
ENV_FILE_MODE = 0o600


@dataclass(frozen=True)
class InitResult:
    """Paths created (or that would be created) and paths left untouched."""

    created: tuple[Path, ...]
    skipped: tuple[Path, ...]


def _template_text(name: str) -> str:
    """Read one packaged template from ``orchestrator.templates``."""
    return (
        resources.files("orchestrator.templates")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def init_layout(
    *,
    config_dir: Path,
    data_dir: Path,
    env_file: Path,
    force: bool = False,
    dry_run: bool = False,
) -> InitResult:
    """Materialise the configuration, env file, and data directories.

    Existing files are skipped unless ``force`` is set.  ``dry_run`` reports
    the actions without creating or writing anything.
    """
    config_dir = Path(config_dir).expanduser()
    data_dir = Path(data_dir).expanduser()
    env_file = Path(env_file).expanduser()
    directories = (config_dir, data_dir, *(data_dir / name for name in DATA_SUBDIRECTORIES))

    created: list[Path] = []
    for directory in directories:
        if directory.exists():
            continue
        if not dry_run:
            directory.mkdir(parents=True, exist_ok=True)
        created.append(directory)

    skipped: list[Path] = []
    config_file = config_dir / CONFIG_FILE_NAME
    targets = (
        (CONFIG_TEMPLATE, config_file, None),
        (ENV_TEMPLATE, env_file, ENV_FILE_MODE),
    )
    for template, target, mode in targets:
        if target.exists() and not force:
            skipped.append(target)
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_template_text(template), encoding="utf-8")
            if mode is not None:
                target.chmod(mode)
        created.append(target)

    return InitResult(created=tuple(created), skipped=tuple(skipped))
