"""Config CLI commands.

Commands:
- archie config migrate: rewrite old ``tools:``/``auth:`` sections into ``plugins:``
"""

import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import click
import yaml
from archie_shared.config import ConfigError, home_dir
from archie_shared.plugins.migrate import MigrationError, migrate_config


@click.group()
def config():
    """Manage the Nexus config file."""


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = path.with_name(f"{path.name}.bak-{stamp}")
    candidate = base
    n = 0
    while candidate.exists():
        n += 1
        candidate = base.with_name(f"{base.name}-{n}")
    return candidate


@config.command()
@click.option(
    "--path",
    "path_opt",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Config file to migrate (default: <ARCHIE_HOME_DIR>/config.yaml).",
)
def migrate(path_opt: Path | None):
    """Convert old 'tools' and 'auth' sections into 'plugins'.

    Writes a timestamped backup next to the file. Running it again is a no-op.
    """
    path = path_opt if path_opt is not None else home_dir() / "config.yaml"
    if not path.exists():
        click.echo(f"No config file at {path}; nothing to migrate.")
        return

    original = path.read_bytes()
    try:
        raw = yaml.safe_load(original)
    except yaml.YAMLError as e:
        raise click.ClickException(f"Invalid YAML in {path}: {e}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise click.ClickException(f"{path}: top level must be a mapping")

    try:
        result = migrate_config(raw)
    except MigrationError as e:
        raise click.ClickException(f"{path}: {e}") from None
    if not result.changed:
        click.echo(f"{path} is already migrated.")
        return

    text = yaml.safe_dump(result.data, sort_keys=False, default_flow_style=False)
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        from archie_shared.schemas import load_nexus_config

        try:
            load_nexus_config(tmp)
        except ConfigError as e:
            raise click.ClickException(
                f"Migrated config is invalid, nothing written: {e}"
            ) from None
        os.chmod(tmp, mode)
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    for note in result.notes:
        click.echo(note)
    click.echo(f"Migrated {path}; backup at {backup}.")
