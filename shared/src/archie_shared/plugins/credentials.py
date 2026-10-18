"""Generate credential providers and credential structs from plugin manifests (D4).

``archie_shared.credentials.providers``/``models`` call these at import time to build
``PROVIDERS``/``CREDENTIAL_TYPES``, so this module must not import them at module
level: the credential imports below are deferred to call time to avoid a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec

from archie_shared.plugins.manifest import LoadedPlugin

if TYPE_CHECKING:
    from archie_shared.credentials.providers import OAuthProvider, StaticProvider


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _credential_spec(loaded: LoadedPlugin):
    spec = loaded.manifest.credential
    if spec is None:
        raise ValueError(f"plugin {loaded.name!r} declares no credential")
    return spec


def provider_from_manifest(loaded: LoadedPlugin) -> StaticProvider | OAuthProvider:
    """Return the credential provider declared by ``loaded``'s ``credential`` block."""
    from archie_shared.credentials.providers import OAuthProvider, StaticProvider

    spec = _credential_spec(loaded)
    if spec.kind == "static":
        # M4 moves site to plugins.<id>.settings
        site_setting = loaded.manifest.settings.get("site")
        site = None
        if site_setting is not None and isinstance(site_setting.default, str):
            site = site_setting.default
        return StaticProvider(
            name=loaded.name,
            fields=list(spec.fields),
            site=site,
            env=dict(spec.env),
            set_env=spec.set_env,
            login=spec.login,
        )
    return OAuthProvider(
        name=loaded.name,
        server_url=spec.server_url,
        authorization_endpoint=spec.authorization_endpoint,
        token_endpoint=spec.token_endpoint,
        registration_endpoint=spec.registration_endpoint,
        scopes=None if spec.scopes is None else list(spec.scopes),
        token_path=spec.token_path,
        refresh_token_path=spec.refresh_token_path,
        expires_in_path=spec.expires_in_path,
        extra_params=None if spec.extra_params is None else dict(spec.extra_params),
    )


def credential_type_from_manifest(loaded: LoadedPlugin) -> type:
    """Return the credential struct type for ``loaded``'s ``credential`` block.

    Static: a generated ``<Camel>Credential`` struct with one optional ``str`` field per
    declared field (unknown fields rejected). OAuth: the shared ``OAuthCredential``.
    """
    spec = _credential_spec(loaded)
    if spec.kind == "oauth":
        from archie_shared.credentials.models import OAuthCredential

        return OAuthCredential
    return msgspec.defstruct(
        f"{_camel(loaded.name)}Credential",
        [(field, str | None, None) for field in spec.fields],
        forbid_unknown_fields=True,
    )


def credential_plugins(plugins: dict[str, LoadedPlugin]) -> list[LoadedPlugin]:
    """Plugins that declare a credential, sorted by id."""
    return [plugins[pid] for pid in sorted(plugins) if plugins[pid].manifest.credential]
