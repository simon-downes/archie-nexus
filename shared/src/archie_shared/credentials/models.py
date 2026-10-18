"""Credential model structs.

``BedrockCredential`` (core) and ``OAuthCredential`` (all OAuth providers) are
hand-written; static plugin credentials are generated from their manifests.
All fields are Optional (default None) so partially-populated entries load
without error. forbid_unknown_fields=True catches typos/stale fields.
"""

import msgspec

from archie_shared.plugins.credentials import credential_plugins, credential_type_from_manifest
from archie_shared.plugins.manifest import load_manifests


class BedrockCredential(msgspec.Struct, forbid_unknown_fields=True):
    """AWS credentials for Bedrock API access."""

    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None


class OAuthCredential(msgspec.Struct, forbid_unknown_fields=True):
    """Shared shape for OAuth services (notion, slack, google).

    Includes discoverable/registrable OAuth fields so that endpoints and
    client_id obtained during login can be stored in the credential entry.
    """

    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None


def _generated_types() -> dict[str, type]:
    plugins = load_manifests()
    return {
        loaded.name: credential_type_from_manifest(loaded) for loaded in credential_plugins(plugins)
    }


# Registry: service name → credential struct type. ``bedrock`` is core; the rest are
# generated from plugin manifests (static → ``<Camel>Credential``, oauth → OAuthCredential).
CREDENTIAL_TYPES: dict[str, type] = {
    "bedrock": BedrockCredential,
    **_generated_types(),
}


def credential_type(name: str) -> type:
    """Return the credential struct type for provider ``name`` (KeyError if unknown)."""
    return CREDENTIAL_TYPES[name]
