"""Current credential and settings environment for tool subprocesses.

The orchestrator used to pass ``runtime_environment()`` to ``docker run`` as
``-e`` flags, which froze the values at container start. Shell commands and
exec runners now get them computed when each subprocess starts, so a new
``archie auth login`` or a ``config.yaml`` edit reaches running sessions.

The computed values are cached until the modification time or size of
``config.yaml`` or ``credentials.yaml`` changes.
"""

from __future__ import annotations

import os
from pathlib import Path

_Key = tuple[tuple[int, int] | None, ...]
_cache: tuple[_Key, dict[str, str], str | None] | None = None
_last_good: dict[str, str] | None = None


def _stat_key(paths: list[Path]) -> _Key:
    key: list[tuple[int, int] | None] = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            key.append(None)
        else:
            key.append((st.st_mtime_ns, st.st_size))
    return tuple(key)


def _compute() -> tuple[dict[str, str], str | None]:
    from archie_shared.config import ConfigError
    from archie_shared.credentials.runtime import runtime_environment
    from archie_shared.schemas import load_nexus_config

    try:
        return runtime_environment(load_nexus_config()), None
    except Exception as exc:  # noqa: BLE001 — a broken config must not block commands
        # Config errors name config.yaml keys. Other errors may come from the
        # credential store, whose YAML errors can quote a line of secrets.
        reason = str(exc) if isinstance(exc, ConfigError) else type(exc).__name__
        return {}, f"credentials and plugin settings were not loaded: {reason}"


def runtime_env() -> tuple[dict[str, str], str | None]:
    """Return ``(variables, warning)`` from the current config and credential store.

    If the files can't be read, the last good variables stay in force with no
    warning, as the live tool policy keeps its last valid snapshot. With no
    good variables yet, they are empty and ``warning`` says why.
    """
    global _cache, _last_good
    from archie_shared.config import home_dir

    home = home_dir()
    key = _stat_key([home / "config.yaml", home / "credentials.yaml"])
    if _cache is None or _cache[0] != key:
        env, warning = _compute()
        if warning is None:
            _last_good = env
        elif _last_good is not None:
            env, warning = _last_good, None
        _cache = (key, env, warning)
    return dict(_cache[1]), _cache[2]


def subprocess_env() -> tuple[dict[str, str], str | None]:
    """Return the process environment overlaid with :func:`runtime_env`."""
    extra, warning = runtime_env()
    return {**os.environ, **extra}, warning
