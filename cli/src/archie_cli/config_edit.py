"""Comment-preserving edits to ``config.yaml``.

``ruamel.yaml`` round-trips the file, so hand-written comments and formatting
outside the changed keys survive. The result is validated with
``load_nexus_config`` before it replaces the original.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


class ConfigEditError(Exception):
    """Raised when ``config.yaml`` can't be updated."""


def _child_map(parent: CommentedMap, key: str, where: str) -> CommentedMap:
    value = parent.get(key)
    if value is None:
        value = CommentedMap()
        parent[key] = value
    if not isinstance(value, dict):
        raise ConfigEditError(f"{where}.{key} is not a mapping")
    return value


def set_plugin_settings(path: Path, plugin_id: str, values: dict[str, Any]) -> None:
    """Set ``plugins.<plugin_id>.settings.<name>`` for each item in ``values``.

    Creates missing parent mappings, and the file itself when absent.

    Raises:
        ConfigEditError: If the file isn't a YAML mapping, a parent key isn't
            a mapping, or the edited config fails validation.
    """
    from archie_shared.config import ConfigError
    from archie_shared.schemas import load_nexus_config

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long existing values
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = yaml.load(text) if text.strip() else None
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise ConfigEditError(f"{path} is not a YAML mapping")

    settings = _child_map(
        _child_map(_child_map(data, "plugins", "config"), plugin_id, "plugins"),
        "settings",
        f"plugins.{plugin_id}",
    )
    for name, value in values.items():
        settings[name] = value

    out = StringIO()
    yaml.dump(data, out)

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".yaml")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(out.getvalue())
        os.chmod(tmp, mode)
        try:
            load_nexus_config(tmp)
        except ConfigError as exc:
            raise ConfigEditError(str(exc).replace(str(tmp), str(path))) from None
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
