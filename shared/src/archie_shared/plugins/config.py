"""The ``plugins:`` section of ``config.yaml``: structs, validation and snapshots.

``plugins: {<id>: {enabled, policy: {read?, write?}, auth: {...}, settings: {...}}}``
is validated against each plugin's manifest (D7, D15). This module must not import
``archie_shared.schemas`` (schemas imports it).
"""

from __future__ import annotations

from typing import Any

import msgspec

from archie_shared.plugins.manifest import LoadedPlugin
from archie_shared.plugins.schema import to_builtins


class PolicyBlockConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Config override for one policy block; ``None`` keeps the manifest default."""

    enabled: bool | None = None
    scope: Any = None


class PluginPolicyConfig(msgspec.Struct, forbid_unknown_fields=True):
    read: PolicyBlockConfig | None = None
    write: PolicyBlockConfig | None = None


class PluginAuthConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Overrides for the plugin's credential provider (``None`` = provider value)."""

    client_id: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    server_url: str | None = None
    scopes: list[str] | None = None
    token_path: str | None = None
    refresh_token_path: str | None = None
    expires_in_path: str | None = None
    extra_params: dict[str, str] | None = None
    env: dict[str, str] | None = None
    set_env: bool | None = None


class PluginConfig(msgspec.Struct, forbid_unknown_fields=True):
    enabled: bool = True
    policy: PluginPolicyConfig = msgspec.field(default_factory=PluginPolicyConfig)
    auth: PluginAuthConfig = msgspec.field(default_factory=PluginAuthConfig)
    settings: dict[str, Any] = msgspec.field(default_factory=dict)


def _is_empty_scope(scope: Any) -> bool:
    return scope is None or scope == [] or scope == {}


def _validate_one(pid: str, config: PluginConfig, loaded: LoadedPlugin) -> None:
    prefix = f"plugins.{pid}"
    try:
        msgspec.convert(config.settings, loaded.settings_type)
    except msgspec.ValidationError as e:
        raise ValueError(f"{prefix}.settings: {e}") from None

    policy = loaded.policy
    blocks = {"read": config.policy.read, "write": config.policy.write}
    if policy is None:
        given = [name for name, block in blocks.items() if block is not None]
        if given:
            raise ValueError(f"{prefix}.policy: plugin {pid!r} has no tool policy")
    else:
        if blocks["write"] is not None and policy.write is None:
            raise ValueError(f"{prefix}.policy.write: plugin {pid!r} has no write tools")
        for name, block in blocks.items():
            if block is None or _is_empty_scope(block.scope):
                continue
            if loaded.scope_type is None:
                raise ValueError(f"{prefix}.policy.{name}.scope: plugin {pid!r} has no scope")
            try:
                msgspec.convert(block.scope, loaded.scope_type)
            except msgspec.ValidationError as e:
                raise ValueError(f"{prefix}.policy.{name}.scope: {e}") from None

    credential = loaded.manifest.credential
    if config.auth != PluginAuthConfig():
        if credential is None:
            raise ValueError(f"{prefix}.auth: plugin {pid!r} declares no credential")
        if credential.kind == "oauth":
            bad = [f for f in ("env", "set_env") if getattr(config.auth, f) is not None]
            if bad:
                raise ValueError(
                    f"{prefix}.auth: {', '.join(bad)} not allowed for an oauth credential"
                )


def validate_plugins(plugins: dict[str, PluginConfig], manifests: dict[str, LoadedPlugin]) -> None:
    """Validate a ``plugins:`` section against manifests; raise ``ValueError``."""
    for pid in plugins:
        if pid not in manifests:
            raise ValueError(f"plugins.{pid}: unknown plugin {pid!r}")
    for pid, config in plugins.items():
        _validate_one(pid, config, manifests[pid])


def resolved_settings(
    plugin_id: str, plugins: dict[str, PluginConfig], manifests: dict[str, LoadedPlugin]
) -> dict[str, Any]:
    """Return ``plugin_id``'s settings: manifest defaults merged with config, as builtins."""
    loaded = manifests[plugin_id]
    config = plugins.get(plugin_id)
    values = config.settings if config is not None else {}
    return to_builtins(msgspec.convert(values, loaded.settings_type))


def _block(override: PolicyBlockConfig | None, default_enabled: bool) -> dict[str, Any]:
    if override is None:
        return {"enabled": default_enabled, "scope": None}
    enabled = default_enabled if override.enabled is None else override.enabled
    return {"enabled": enabled, "scope": override.scope}


def policy_snapshot(
    plugins: dict[str, PluginConfig], manifests: dict[str, LoadedPlugin]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Detached non-secret policy snapshot for every plugin with a tool policy.

    A disabled plugin gets both blocks disabled: callers that fall back to the
    read-enabled default for a missing entry must not see it as allowed.
    """
    snapshot: dict[str, dict[str, dict[str, Any]]] = {}
    for pid in sorted(manifests):
        loaded = manifests[pid]
        spec = loaded.policy
        config = plugins.get(pid) or PluginConfig()
        if spec is None:
            continue
        if not config.enabled:
            off = {"enabled": False, "scope": None}
            snapshot[pid] = {"read": dict(off), "write": dict(off)}
            continue
        read = _block(config.policy.read, spec.read.enabled)
        if spec.write is None:
            write = {"enabled": False, "scope": None}
        else:
            write = _block(config.policy.write, spec.write.enabled)
        snapshot[pid] = {"read": read, "write": write}
    return snapshot
