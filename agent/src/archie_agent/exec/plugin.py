"""Plugin SDK: the API plugin code under ``plugins/<id>/`` imports.

Tool layer only — no harness imports. Plugin tools register here, separate from
the core ``exec.tools`` registry, so they never reach native specs, exec docs
or guidelines (D6, D22).
"""

from collections.abc import Callable

from archie_shared.plugins import PLUGIN_PACKAGE

from archie_agent.exec.tools import ToolError

__all__ = ["ToolError", "plugin_tools", "tool"]

_PLUGIN_TOOLS: dict[str, Callable] = {}


def _plugin_id(module: str) -> str:
    """Return ``<id>`` for a module named ``archie_plugins.<id>[.<...>]``."""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != PLUGIN_PACKAGE or not parts[1]:
        raise ValueError(
            f"plugin tool defined in module {module!r}; plugin tools must live under "
            f"'{PLUGIN_PACKAGE}.<id>'"
        )
    return parts[1]


def tool(fn: Callable | None = None, *, namespace: str):
    """Register an async function as a plugin exec tool under ``namespace``.

    Usage:
        @tool(namespace="datadog")
        async def search_monitors(query): ...
    """

    def decorator(f: Callable) -> Callable:
        plugin = _plugin_id(f.__module__)
        key = f"{namespace}.{f.__name__}"
        if key in _PLUGIN_TOOLS:
            raise ValueError(f"Duplicate plugin tool registration: {key}")
        f._namespace = namespace
        f._plugin = plugin
        _PLUGIN_TOOLS[key] = f
        return f

    if fn is not None:
        return decorator(fn)
    return decorator


def plugin_tools() -> dict[str, Callable]:
    """Return a copy of the plugin tool registry (``"ns.fn"`` -> function)."""
    return dict(_PLUGIN_TOOLS)
