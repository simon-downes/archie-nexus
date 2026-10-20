"""Resolve selected stored credentials and plugin settings into agent runtime env vars."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig

from archie_shared.credentials.api import provider_override
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.credentials.providers import PROVIDERS, StaticProvider, effective_provider
from archie_shared.credentials.store import get_credential
from archie_shared.plugins.config import enabled_plugins, is_enabled, resolved_settings
from archie_shared.plugins.manifest import load_manifests

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _claim(owners: dict[str, str], env_name: str, owner: str) -> None:
    previous = owners.get(env_name)
    if previous and previous != owner:
        raise ValueError(
            f"Environment variable '{env_name}' is mapped by both '{previous}' and '{owner}'"
        )
    owners[env_name] = owner


def runtime_environment(config: NexusConfig) -> dict[str, str]:
    """Return env vars for a new agent container.

    Static credentials are projected for providers with ``set_env`` enabled;
    missing or partial credentials are skipped (field validation is the API's
    job). Plugin settings fields that declare ``env`` (top-level scalars only,
    enforced at manifest load) are projected when their resolved value is not
    None. Disabled plugins project nothing.
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
        env_fields = {
            fname: spec.env
            for fname, spec in manifests[pid].manifest.settings.items()
            if spec.env is not None
        }
        if not env_fields:
            continue
        settings = resolved_settings(pid, config.plugins, manifests)
        for fname, env_name in env_fields.items():
            _claim(owners, env_name, f"{pid}.settings.{fname}")
            value = settings.get(fname)
            if value is not None:
                result[env_name] = str(value)
    return result
