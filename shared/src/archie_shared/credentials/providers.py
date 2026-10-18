"""Provider registry — service descriptors.

Defines how each service authenticates (static token vs OAuth) and the
configuration needed for OAuth flows (endpoints, scopes, etc.). ``bedrock`` is
defined here; every other provider comes from a ``plugins/<id>/plugin.yaml``
``credential`` block.
"""

import msgspec

from archie_shared.credentials.api import AuthProviderOverride
from archie_shared.plugins.credentials import credential_plugins, provider_from_manifest
from archie_shared.plugins.manifest import load_manifests


class StaticProvider(msgspec.Struct, forbid_unknown_fields=True):
    """A service that uses static credentials (tokens, API keys).

    Attributes:
        name: Service name (matches CREDENTIAL_TYPES key).
        fields: List of credential field names the user must provide.
        login: Login method; ``"aws_chain"`` reads fields from the standard AWS
            credential chain (in ``fields`` order: access key, secret, session token).
        can_refresh_noninteractive: Whether credentials can be refreshed
            without user interaction. Always False for static providers.
    """

    name: str
    fields: list[str]
    site: str | None = None
    env: dict[str, str] = msgspec.field(default_factory=dict)
    set_env: bool = False
    login: str | None = None
    can_refresh_noninteractive: bool = False


class OAuthProvider(msgspec.Struct, forbid_unknown_fields=True):
    """A service that uses OAuth2 + PKCE for authentication.

    Attributes:
        name: Service name.
        server_url: OAuth resource server URL for endpoint discovery (RFC 9470).
            None if endpoints are specified directly.
        authorization_endpoint: OAuth authorization endpoint (direct or discovered).
        token_endpoint: OAuth token endpoint (direct or discovered).
        scopes: OAuth scopes to request.
        token_path: Dot-path to extract access_token from token response.
        refresh_token_path: Dot-path to extract refresh_token from token response.
        extra_params: Additional parameters for the authorization request.
        can_refresh_noninteractive: Whether tokens can be refreshed without
            user interaction (True if service issues refresh tokens).
    """

    name: str
    server_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    scopes: list[str] | None = None
    token_path: str = "access_token"
    refresh_token_path: str = "refresh_token"
    expires_in_path: str = "expires_in"
    extra_params: dict[str, str] | None = None
    can_refresh_noninteractive: bool = True


# --- Provider registry ---


def effective_provider(
    name: str, override: "AuthProviderOverride | None" = None
) -> StaticProvider | OAuthProvider:
    """Return a code-defined provider with optional typed OAuth overrides."""
    provider = PROVIDERS[name]
    if override is None:
        return provider
    values = {field: getattr(provider, field) for field in provider.__struct_fields__}
    for field in provider.__struct_fields__:
        if hasattr(override, field):
            value = getattr(override, field)
            if value is not None:
                values[field] = value
    if isinstance(provider, StaticProvider):
        if override.env is not None:
            values["env"] = override.env
        if override.set_env is not None:
            values["set_env"] = override.set_env
    return type(provider)(**values)


def _generated_providers() -> dict[str, StaticProvider | OAuthProvider]:
    plugins = load_manifests()
    return {loaded.name: provider_from_manifest(loaded) for loaded in credential_plugins(plugins)}


# Core provider (D10): bedrock has no plugin manifest. Every other provider is
# generated from its plugin's ``credential`` block, ordered by plugin id.
PROVIDERS: dict[str, StaticProvider | OAuthProvider] = {
    "bedrock": StaticProvider(
        name="bedrock",
        fields=["aws_access_key_id", "aws_secret_access_key", "aws_session_token"],
        env={
            "aws_access_key_id": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "aws_session_token": "AWS_SESSION_TOKEN",
        },
        set_env=False,
        login="aws_chain",
        can_refresh_noninteractive=False,
    ),
    **_generated_providers(),
}
