"""Convert the old ``tools:``/``auth:`` config sections into ``plugins:`` (070 D1).

``migrate_config()`` is pure: it takes the parsed YAML mapping and returns the new
mapping. It does not validate against manifests; ``archie config migrate`` does
that by loading the result with ``load_nexus_config()`` before writing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

_LINEAR_NOTE = "dropped linear (provider removed)"

# auth.providers.<id>.<field> values that are now plugin settings (D8, github work_orgs).
_SETTINGS = {("github", "work_orgs"), ("datadog", "site")}


class MigrationError(Exception):
    """The old config cannot be converted; the message names the offending key."""


@dataclass(frozen=True)
class MigrationResult:
    data: dict
    changed: bool
    notes: list[str] = field(default_factory=list)


def _mapping(value: Any, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MigrationError(f"{key}: expected a mapping")
    return value


def _put(plugins: dict, path: list[str], value: Any) -> None:
    """Set ``plugins.<path>`` to ``value``; an existing different value is a conflict."""
    node = plugins
    for i, part in enumerate(path[:-1]):
        child = node.setdefault(part, {})
        if child is None:
            child = node[part] = {}
        if not isinstance(child, dict):
            dotted = ".".join(["plugins", *path[: i + 1]])
            raise MigrationError(f"{dotted}: expected a mapping")
        node = child
    last = path[-1]
    if last in node and node[last] != value:
        dotted = ".".join(["plugins", *path])
        raise MigrationError(f"{dotted}: already set to a different value")
    node[last] = copy.deepcopy(value)


def migrate_config(raw: dict) -> MigrationResult:
    """Return ``raw`` with ``tools``/``auth`` moved into ``plugins``.

    Raises:
        MigrationError: for a key with no new location or a conflicting ``plugins`` value.
    """
    if "tools" not in raw and "auth" not in raw:
        return MigrationResult(data=raw, changed=False, notes=[])

    plugins = copy.deepcopy(_mapping(raw.get("plugins"), "plugins"))
    notes: list[str] = []

    def drop_linear() -> None:
        if _LINEAR_NOTE not in notes:
            notes.append(_LINEAR_NOTE)

    auth = _mapping(raw.get("auth"), "auth")
    for key in auth:
        if key != "providers":
            raise MigrationError(f"auth.{key}: no equivalent under plugins; remove it")
    providers = _mapping(auth.get("providers"), "auth.providers")
    for pid, fields in providers.items():
        if pid == "linear":
            drop_linear()
            continue
        if pid == "bedrock":
            raise MigrationError(
                "auth.providers.bedrock: bedrock has no config overrides any more; remove it"
            )
        for name, value in _mapping(fields, f"auth.providers.{pid}").items():
            if (pid, name) in _SETTINGS:
                _put(plugins, [pid, "settings", name], value)
            elif name == "site":
                raise MigrationError(
                    f"auth.providers.{pid}.site: only datadog has a site setting; remove it"
                )
            else:
                _put(plugins, [pid, "auth", name], value)

    tools = _mapping(raw.get("tools"), "tools")
    for pid, policy in tools.items():
        if pid == "linear":
            drop_linear()
            continue
        if not isinstance(policy, dict):
            raise MigrationError(f"tools.{pid}: expected a mapping")
        _put(plugins, [pid, "policy"], policy)

    data: dict = {}
    for key, value in raw.items():
        if key == "tools":
            continue
        if key == "auth":
            if "plugins" not in raw:
                data["plugins"] = plugins
            continue
        data[key] = plugins if key == "plugins" else value
    if "plugins" not in data:
        data["plugins"] = plugins
    return MigrationResult(data=data, changed=True, notes=notes)
