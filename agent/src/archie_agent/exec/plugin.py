"""Plugin SDK: the API plugin code under ``plugins/<id>/`` imports.

Tool layer only — no harness imports. Plugin tools register here, separate from
the core ``exec.tools`` registry, so they never reach native specs, exec docs
or guidelines (D6, D22). Plugin code gets its credentials, policy, settings and
cache from the host-supplied ``context()`` (D12, D13, D14, D24).
"""

from __future__ import annotations

import copy
import types
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archie_shared.plugins import PLUGIN_PACKAGE

from archie_agent.exec.tools import ToolError
from archie_agent.exec.tools.cache import ToolCache

if TYPE_CHECKING:
    from archie_shared.plugins import LoadedPlugin

__all__ = [
    "CredentialSource",
    "PluginContext",
    "PluginContextError",
    "PluginPolicy",
    "PolicyBlock",
    "ToolError",
    "build_context",
    "context",
    "install_context",
    "plugin_tools",
    "require_read",
    "require_write",
    "tool",
    "use_context",
]

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


# --- Runtime context --------------------------------------------------------


class PluginContextError(ToolError):
    """Plugin code ran without a host-installed ``PluginContext``."""

    def __init__(
        self, message: str = "no plugin context: plugin code must run inside an exec host"
    ):
        super().__init__(message)


@dataclass(frozen=True)
class PolicyBlock:
    """One resolved policy block; ``scope`` None/[]/{} means no scope."""

    enabled: bool
    scope: Any = None


@dataclass(frozen=True)
class PluginPolicy:
    """Resolved read/write policy; ``write`` is None when the plugin has no writes."""

    read: PolicyBlock
    write: PolicyBlock | None


class CredentialSource:
    """Reads one plugin's own provider credential (provider id == plugin id, D13).

    ``get()`` reads the credential store on every call (D24), so tokens refreshed
    mid-exec are seen. ``loader`` (zero-arg callable) replaces the store in tests.
    """

    __slots__ = ("_loader", "_plugin")

    def __init__(self, plugin: str, loader: Callable[[], Any] | None = None):
        self._plugin = plugin
        self._loader = loader

    @property
    def plugin(self) -> str:
        return self._plugin

    def get(self) -> Any | None:
        """Return this plugin's credential struct, or None if none is stored."""
        if self._loader is not None:
            return self._loader()
        from archie_shared.credentials.store import get_credential

        return get_credential(self._plugin)

    def __repr__(self) -> str:
        return f"CredentialSource({self._plugin!r})"


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin call may use, supplied by the exec host (D12)."""

    plugin: str
    settings: Mapping[str, Any]
    policy: PluginPolicy
    credentials: CredentialSource
    cache: ToolCache
    workspace: Path | None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        settings = self.settings
        if not isinstance(settings, types.MappingProxyType):
            settings = types.MappingProxyType(copy.deepcopy(dict(settings)))
        object.__setattr__(self, "settings", settings)


_CURRENT: ContextVar[PluginContext | None] = ContextVar("archie_plugin_context", default=None)


def context() -> PluginContext:
    """Return the current plugin context; fail closed when none is installed."""
    ctx = _CURRENT.get()
    if ctx is None:
        raise PluginContextError()
    return ctx


@contextmanager
def use_context(ctx: PluginContext) -> Iterator[PluginContext]:
    """Install ``ctx`` for the enclosed block (reset by token; same task/context only)."""
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def install_context(ctx: PluginContext | None) -> None:
    """Set the current context by value (for tests whose fixtures run in another context)."""
    _CURRENT.set(ctx)


def _has_scope(scope: Any) -> bool:
    return not (scope is None or scope == [] or scope == {})


def _gate(
    block: PolicyBlock | None,
    kind: str,
    error: type[Exception],
    message: str,
    scope_supported: bool,
    scope_message: str | None,
    plugin: str,
) -> PolicyBlock:
    if block is None or not block.enabled:
        raise error(message)
    if not scope_supported and _has_scope(block.scope):
        raise error(scope_message or f"{plugin} {kind} scope is not supported; remove it")
    return block


def require_read(
    error: type[Exception],
    message: str,
    *,
    scope_supported: bool = False,
    scope_message: str | None = None,
) -> PolicyBlock:
    """Return the current plugin's read block; raise ``error(message)`` when disabled.

    Without ``scope_supported`` a non-empty scope raises ``error(scope_message)``.
    """
    ctx = context()
    return _gate(
        ctx.policy.read, "read", error, message, scope_supported, scope_message, ctx.plugin
    )


def require_write(
    error: type[Exception],
    message: str,
    *,
    scope_supported: bool = False,
    scope_message: str | None = None,
) -> PolicyBlock:
    """Return the current plugin's write block; raise ``error(message)`` when disabled.

    A plugin with no write block (``policy.write is None``) is always disabled.
    """
    ctx = context()
    return _gate(
        ctx.policy.write, "write", error, message, scope_supported, scope_message, ctx.plugin
    )


def _resolve_block(raw: Any, default_enabled: bool) -> PolicyBlock:
    if not isinstance(raw, dict):
        return PolicyBlock(enabled=default_enabled)
    enabled = raw.get("enabled")
    return PolicyBlock(
        enabled=default_enabled if enabled is None else bool(enabled),
        scope=copy.deepcopy(raw.get("scope")),
    )


def resolve_policy(manifest: LoadedPlugin, entry: Any) -> PluginPolicy:
    """Build a ``PluginPolicy`` from a snapshot entry, falling back to manifest defaults.

    A plugin without a tool policy gets reads disabled and no writes (fail closed).
    """
    spec = manifest.policy
    if spec is None:
        return PluginPolicy(read=PolicyBlock(enabled=False), write=None)
    entry = entry if isinstance(entry, dict) else {}
    read = _resolve_block(entry.get("read"), spec.read.enabled)
    write = None if spec.write is None else _resolve_block(entry.get("write"), spec.write.enabled)
    return PluginPolicy(read=read, write=write)


def build_context(
    plugin_id: str,
    *,
    manifest: LoadedPlugin,
    policy_snapshot: dict,
    settings: dict,
    home: Path | None = None,
    workspace: Path | None,
    clock: Callable[[], datetime] | None = None,
    credentials: CredentialSource | None = None,
) -> PluginContext:
    """Build ``plugin_id``'s context from the runner's policy snapshot and settings."""
    from archie_shared.config import home_dir

    clock = clock or (lambda: datetime.now(UTC))
    snapshot = policy_snapshot if isinstance(policy_snapshot, dict) else {}
    cache = ToolCache(
        root=(home if home is not None else home_dir()) / "cache" / plugin_id,
        clock=clock,
        max_bytes=manifest.manifest.cache.max_bytes,
    )
    return PluginContext(
        plugin=plugin_id,
        settings=settings,
        policy=resolve_policy(manifest, snapshot.get(plugin_id)),
        credentials=credentials if credentials is not None else CredentialSource(plugin_id),
        cache=cache,
        workspace=workspace,
        clock=clock,
    )
