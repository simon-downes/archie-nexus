"""Current credential and settings environment for tool subprocesses.

The orchestrator used to pass ``runtime_environment()`` to ``docker run`` as
``-e`` flags, which froze the values at container start. Shell commands and
exec runners now get them computed when each subprocess starts, from the config
and credentials the agent holds (``host_config``, plan 090), so a new
``archie auth login`` or a config edit reaches running sessions after a sync.

The computed values are cached until the held config or credentials change
(``HostConfig.generation``). A compute that fails is tried again after
:data:`RETRY_AFTER` seconds, since a failed credential fetch changes nothing held.
"""

from __future__ import annotations

import os
import time

from archie_agent import host_config

RETRY_AFTER = 30.0
"""Seconds before a failed compute (orchestrator unreachable, bad entry) is retried."""
_clock = time.monotonic

_Key = tuple[int, int] | None
# (key, variables, warning, monotonic time to retry, or None after a success)
_cache: tuple[_Key, dict[str, str], str | None, float | None] | None = None
_last_good: dict[str, str] | None = None
_inherited = False


def inherit_environment() -> None:
    """Use this process's environment as it is, adding nothing.

    For the exec runner: the agent built its environment with :func:`subprocess_env`,
    and the runner has no ``host_config`` of its own (no agent token).
    """
    global _inherited
    _inherited = True


def _key() -> _Key:
    """Identifies what the variables were computed from; None when nothing is held."""
    try:
        host = host_config.current()
    except Exception:  # noqa: BLE001 — the compute reports why
        return None
    return (id(host), host.generation)


def _compute() -> tuple[dict[str, str], str | None]:
    from archie_shared.config import ConfigError
    from archie_shared.credentials.runtime import runtime_environment

    try:
        host = host_config.current()
        return runtime_environment(host.config(), lookup=host.get_credential), None
    except Exception as exc:  # noqa: BLE001 — a broken config must not block commands
        # Config and orchestrator errors name config keys or providers. Other errors
        # may quote a credential, so only their type is shown.
        quoted = isinstance(exc, ConfigError | host_config.HostConfigError)
        reason = str(exc) if quoted else type(exc).__name__
        return {}, f"credentials and plugin settings were not loaded: {reason}"


def runtime_env() -> tuple[dict[str, str], str | None]:
    """Return ``(variables, warning)`` from the held config and credentials.

    If they can't be computed, the last good variables stay in force with no
    warning, as the live tool policy keeps its last valid snapshot. With no
    good variables yet, they are empty and ``warning`` says why.
    """
    global _cache, _last_good
    if _inherited:
        return {}, None
    key = _key()
    now = _clock()
    retry_due = _cache is not None and _cache[3] is not None and now >= _cache[3]
    if _cache is None or key is None or _cache[0] != key or retry_due:
        env, warning = _compute()
        failed = warning is not None
        if not failed:
            _last_good = env
        elif _last_good is not None:
            env, warning = _last_good, None
        # The key from before the compute: credentials it fetched move the generation, so
        # the next call recomputes once from held values instead of missing a change.
        _cache = (key, env, warning, now + RETRY_AFTER if failed else None)
    return dict(_cache[1]), _cache[2]


def subprocess_env() -> tuple[dict[str, str], str | None]:
    """Return the process environment overlaid with :func:`runtime_env`."""
    extra, warning = runtime_env()
    return {**os.environ, **extra}, warning
