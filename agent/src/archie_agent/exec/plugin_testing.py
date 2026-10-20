"""Test helpers for plugin code: build a ``PluginContext`` without an exec host.

Every plugin's tests can import ``make_plugin_context``; anything not given comes
from the plugin's manifest defaults, and the credential is served from memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archie_shared.plugins import load_manifests, resolved_settings

from archie_agent.exec.plugin import CredentialSource, PluginContext, build_context

__all__ = ["make_plugin_context"]

_UNSET = object()


def make_plugin_context(
    plugin_id: str,
    *,
    tmp_path: Path,
    policy: dict | None = None,
    settings: dict | None = None,
    credential: Any = None,
    workspace: Path | None | object = _UNSET,
    clock=None,
    plugins_root: Path | None = None,
) -> PluginContext:
    """Return ``plugin_id``'s context for tests.

    ``policy`` is a runner policy snapshot (``{id: {read, write}}``) — missing entries
    use manifest defaults. ``settings`` are merged over the manifest defaults. The
    cache lives under ``tmp_path/cache/<id>`` and ``workspace`` defaults to ``tmp_path``.
    """
    manifests = load_manifests(plugins_root)
    if plugin_id not in manifests:
        raise KeyError(f"unknown plugin {plugin_id!r}")
    values = {**resolved_settings(plugin_id, {}, manifests), **(settings or {})}
    return build_context(
        plugin_id,
        manifest=manifests[plugin_id],
        policy_snapshot=policy or {},
        settings=values,
        home=tmp_path,
        workspace=tmp_path if workspace is _UNSET else workspace,
        clock=clock,
        credentials=CredentialSource(plugin_id, loader=lambda: credential),
    )
