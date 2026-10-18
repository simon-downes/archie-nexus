"""Plugin loader: install the ``archie_plugins`` package and import plugin entries.

Standalone and side-effect-free until called; no harness imports. Plugin code
under ``plugins/<id>/`` is imported as ``archie_plugins.<id>`` (D2), where
``archie_plugins`` is a synthetic namespace package whose ``__path__`` is the
plugins root — the plugins dir itself is never put on ``sys.path``.
"""

import importlib
import importlib.machinery
import sys
import types
from collections.abc import Callable, Iterable
from pathlib import Path

from archie_shared.config import plugins_dir
from archie_shared.plugins import PLUGIN_PACKAGE, LoadedPlugin

from archie_agent.exec import plugin as _sdk

# Names the runner injects into every exec namespace besides core tools.
RUNNER_BUILTINS = frozenset({"asyncio", "os", "json", "re", "Path"})


class PluginLoadError(Exception):
    """A plugin's code failed to import or registered tools inconsistently."""


def _installed_root() -> Path | None:
    module = sys.modules.get(PLUGIN_PACKAGE)
    if module is None:
        return None
    paths = list(getattr(module, "__path__", []))
    return Path(paths[0]) if paths else None


def install_package(root: Path) -> None:
    """Install ``archie_plugins`` as a package rooted at ``root`` (idempotent)."""
    root = Path(root).resolve()
    current = _installed_root()
    if current is not None:
        if current.resolve() == root:
            return
        raise PluginLoadError(
            f"'{PLUGIN_PACKAGE}' already installed from {current}; cannot reinstall from {root}"
        )
    spec = importlib.machinery.ModuleSpec(PLUGIN_PACKAGE, None, is_package=True)
    spec.submodule_search_locations = [str(root)]
    module = types.ModuleType(PLUGIN_PACKAGE, "Synthetic package holding in-repo plugins.")
    module.__spec__ = spec
    module.__path__ = [str(root)]
    module.__package__ = PLUGIN_PACKAGE
    sys.modules[PLUGIN_PACKAGE] = module


def _reset_for_tests() -> None:
    """Remove ``archie_plugins`` (and submodules) and their registered tools."""
    prefix = PLUGIN_PACKAGE + "."
    for name in [n for n in sys.modules if n == PLUGIN_PACKAGE or n.startswith(prefix)]:
        del sys.modules[name]
    for key, func in list(_sdk._PLUGIN_TOOLS.items()):
        module = getattr(func, "__module__", "") or ""
        if module == PLUGIN_PACKAGE or module.startswith(prefix):
            del _sdk._PLUGIN_TOOLS[key]


def _reserved_names() -> set[str]:
    """Top-level exec names (and core service namespaces) a plugin namespace must not shadow."""
    from archie_agent.exec.tools import get_all_tools

    core = {getattr(func, "_namespace", None) or key for key, func in get_all_tools().items()}
    return core | RUNNER_BUILTINS


def load_plugins(
    manifests: dict[str, LoadedPlugin],
    *,
    root: Path | None = None,
    enabled: Iterable[str] | None = None,
) -> dict[str, dict[str, Callable]]:
    """Import each selected plugin's ``entry`` and return ``{id: {"ns.fn": func}}``."""
    install_package(plugins_dir() if root is None else root)
    allowed = None if enabled is None else set(enabled)
    selected = {
        pid: manifests[pid] for pid in sorted(manifests) if allowed is None or pid in allowed
    }

    reserved = _reserved_names()
    for pid, loaded in selected.items():
        for ns in loaded.manifest.namespaces:
            if ns in reserved:
                raise PluginLoadError(
                    f"Plugin '{pid}' namespace '{ns}' collides with a core exec name"
                )

    for pid, loaded in selected.items():
        entry = loaded.manifest.entry
        if entry is None:
            continue
        try:
            importlib.import_module(entry)
        except Exception as exc:
            raise PluginLoadError(f"Failed to load plugin '{pid}': {exc}") from exc

    result: dict[str, dict[str, Callable]] = {pid: {} for pid in selected}
    for key, func in sorted(_sdk.plugin_tools().items()):
        pid = getattr(func, "_plugin", None)
        if pid not in manifests:
            raise PluginLoadError(f"Tool '{key}' registered by unknown plugin '{pid}'")
        if pid not in selected:
            continue
        ns = getattr(func, "_namespace", None)
        if ns not in selected[pid].manifest.namespaces:
            raise PluginLoadError(
                f"Plugin '{pid}' registers tool '{key}' in undeclared namespace '{ns}'"
            )
        result[pid][key] = func
    return result
