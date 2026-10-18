"""Resolve selected stored credentials and plugin settings into agent runtime env vars."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig

from archie_shared.credentials.api import provider_override
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.credentials.providers import PROVIDERS, StaticProvider, effective_provider
from archie_shared.credentials.store import get_credential
from archie_shared.plugins.config import enabled_plugins, is_enabled, resolved_settings
from archie_shared.plugins.manifest import load_manifests
from archie_shared.plugins.schema import FieldSpec

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SCALARS = (str, int, float, bool)


def _claim(owners: dict[str, str], env_name: str, owner: str) -> None:
    previous = owners.get(env_name)
    if previous and previous != owner:
        raise ValueError(
            f"Environment variable '{env_name}' is mapped by both '{previous}' and '{owner}'"
        )
    owners[env_name] = owner


def _settings_env(
    path: str, fields: dict[str, FieldSpec], values: dict[str, Any]
) -> list[tuple[str, str, Any]]:
    """``(owner, env_name, value)`` for every settings field (nested too) that declares env."""
    result: list[tuple[str, str, Any]] = []
    for fname, spec in fields.items():
        value = values.get(fname) if isinstance(values, dict) else None
        owner = f"{path}.{fname}"
        if spec.env is not None:
            result.append((owner, spec.env, value))
        if spec.fields:
            result.extend(_settings_env(owner, spec.fields, value or {}))
    return result


def runtime_environment(config: NexusConfig) -> dict[str, str]:
    """Return env vars for a new agent container.

    Static credentials are projected for providers with ``set_env`` enabled;
    missing or partial credentials are skipped (field validation is the API's
    job). Plugin settings fields that declare ``env`` are projected when their
    resolved value is not None. Disabled plugins project nothing.
    """
    result: dict[str, str] = {}
    owners: dict[str, str] = {}
    for name in PROVIDERS:
        if not is_enabled(name, config):
            continue
        provider = effective_provider(name, provider_override(config, name))
        if not isinstance(provider, StaticProvider) or not provider.set_env:
            continue
        for field, env_name in provider.env.items():
            if field not in provider.fields:
                raise ValueError(f"Provider '{name}' maps unknown credential field '{field}'")
            if not _ENV_NAME.fullmatch(env_name):
                raise ValueError(f"Provider '{name}' has invalid environment name '{env_name}'")
            _claim(owners, env_name, name)
        credential = get_credential(name, CREDENTIAL_TYPES[name])
        if credential is None:
            continue
        values = {field: getattr(credential, field) for field in credential.__struct_fields__}
        for field, env_name in provider.env.items():
            value = values.get(field)
            if value:
                result[env_name] = value

    manifests = load_manifests()
    for pid in sorted(enabled_plugins(config.plugins, manifests)):
        fields = manifests[pid].manifest.settings
        if not any(spec.env is not None or spec.fields for spec in fields.values()):
            continue
        settings = resolved_settings(pid, config.plugins, manifests)
        for owner, env_name, value in _settings_env(f"{pid}.settings", fields, settings):
            _claim(owners, env_name, owner)
            if value is None:
                continue
            if not isinstance(value, _SCALARS):
                raise ValueError(
                    f"Setting '{owner}' maps to '{env_name}' but is not a scalar "
                    f"({type(value).__name__})"
                )
            result[env_name] = str(value)
    return result
