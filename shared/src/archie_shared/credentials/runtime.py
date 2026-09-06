"""Resolve selected stored credentials into agent runtime environment variables."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig

from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.credentials.providers import PROVIDERS, StaticProvider, effective_provider
from archie_shared.credentials.store import get_credential

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def runtime_environment(config: NexusConfig) -> dict[str, str]:
    """Return configured static credential values for a new agent container.

    Only providers with ``set_env`` enabled are projected. Missing or partial
    credentials are skipped; provider field validation remains the API's job.
    """
    result: dict[str, str] = {}
    owners: dict[str, str] = {}
    for name in PROVIDERS:
        override = config.auth.providers.get(name)
        provider = effective_provider(name, override)
        if not isinstance(provider, StaticProvider) or not provider.set_env:
            continue
        for field, env_name in provider.env.items():
            if field not in provider.fields:
                raise ValueError(f"Provider '{name}' maps unknown credential field '{field}'")
            if not _ENV_NAME.fullmatch(env_name):
                raise ValueError(f"Provider '{name}' has invalid environment name '{env_name}'")
            previous = owners.get(env_name)
            if previous and previous != name:
                raise ValueError(f"Environment variable '{env_name}' is mapped by both '{previous}' and '{name}'")
            owners[env_name] = name
        credential = get_credential(name, CREDENTIAL_TYPES[name])
        if credential is None:
            continue
        values = {field: getattr(credential, field) for field in credential.__struct_fields__}
        for field, env_name in provider.env.items():
            value = values.get(field)
            if value:
                result[env_name] = value
    return result
