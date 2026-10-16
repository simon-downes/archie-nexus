"""Typed, secret-free auth API models shared by Nexus services."""

import msgspec


class StaticProviderResponse(msgspec.Struct, forbid_unknown_fields=True):
    name: str
    auth_type: str = "static"
    fields: list[str] = msgspec.field(default_factory=list)
    site: str | None = None
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


class AuthProviderOverride(msgspec.Struct, forbid_unknown_fields=True):
    site: str | None = None
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
    # github only: owners (orgs or users) whose repositories use github.work_token.
    work_orgs: list[str] | None = None


class AuthConfig(msgspec.Struct, forbid_unknown_fields=True):
    providers: dict[str, AuthProviderOverride] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, override in self.providers.items():
            if name != "github" and override.work_orgs is not None:
                raise ValueError(f"auth.providers.{name}.work_orgs is only valid for github")
