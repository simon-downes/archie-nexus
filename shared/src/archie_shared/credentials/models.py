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


def credential_type_for(service: str) -> type:
    """The registered credential type of ``service``.

    Raises:
        KeyError: If ``service`` isn't a known provider.
    """
    if service not in CREDENTIAL_TYPES:
        known = ", ".join(sorted(CREDENTIAL_TYPES))
        raise KeyError(f"Unknown service '{service}'. Known: {known}")
    return CREDENTIAL_TYPES[service]


def to_credential[T](
    service: str, entry: object, credential_type: type[T] | None = None, *, origin: str
) -> T:
    """Convert one raw stored entry into its typed credential struct.

    Shared by the store and the agent's readers (plan 090), so all report the same errors.

    Args:
        service: Provider name.
        entry: The raw stored entry (a mapping).
        credential_type: Overrides the type from :data:`CREDENTIAL_TYPES`.
        origin: Where the entry came from, for the error (``"in <path>"``,
            ``"from the orchestrator"``).

    Raises:
        KeyError: If ``service`` isn't a known provider and no type is given.
        ValueError: If the entry isn't a mapping or doesn't fit the type.
    """
    if credential_type is None:
        credential_type = credential_type_for(service)
    if not isinstance(entry, dict):
        raise ValueError(f"Credential entry for '{service}' must be a mapping")
    try:
        return msgspec.convert(entry, credential_type, strict=False)
    except msgspec.ValidationError as e:
        raise ValueError(f"Validation error for '{service}' {origin}: {e}") from None
