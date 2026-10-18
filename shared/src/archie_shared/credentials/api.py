"""Typed, secret-free auth API models shared by Nexus services."""

from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec

from archie_shared.plugins.config import PluginAuthConfig

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig


class StaticProviderResponse(msgspec.Struct, forbid_unknown_fields=True):
    name: str
    auth_type: str = "static"
    fields: list[str] = msgspec.field(default_factory=list)
    can_refresh_noninteractive: bool = False
    env: dict[str, str] = msgspec.field(default_factory=dict)
    set_env: bool = False


class OAuthProviderResponse(msgspec.Struct, forbid_unknown_fields=True):
    name: str
    auth_type: str = "oauth"
    server_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    scopes: list[str] = msgspec.field(default_factory=list)
    token_path: str = "access_token"
    refresh_token_path: str = "refresh_token"
    expires_in_path: str = "expires_in"
    extra_params: dict[str, str] = msgspec.field(default_factory=dict)
    can_refresh_noninteractive: bool = True
    client_id: str | None = None
    redirect_uri: str | None = None


class CredentialStatus(msgspec.Struct, forbid_unknown_fields=True):
    provider: str
    auth_type: str
    configured: bool
    state: str
    expires_at: str | None = None
    error: str | None = None


class OAuthFlowStatus(msgspec.Struct, forbid_unknown_fields=True):
    flow_id: str
    provider: str
    status: str
    credential_status: CredentialStatus | None = None
    error: str | None = None


class OAuthLoginResponse(msgspec.Struct, forbid_unknown_fields=True):
    flow_id: str
    authorization_url: str
    redirect_uri: str


def provider_override(config: NexusConfig, name: str) -> PluginAuthConfig | None:
    """Return ``plugins.<name>.auth`` from ``config``, or None if not configured."""
    plugin = config.plugins.get(name)
    return None if plugin is None else plugin.auth
