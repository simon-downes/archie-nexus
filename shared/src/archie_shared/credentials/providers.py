"""Provider registry — code-defined service descriptors.

Defines how each service authenticates (static token vs OAuth) and the
configuration needed for OAuth flows (endpoints, scopes, etc.).
"""

import msgspec

from archie_shared.credentials.api import AuthProviderOverride


class StaticProvider(msgspec.Struct, forbid_unknown_fields=True):
    """A service that uses static credentials (tokens, API keys).

    Attributes:
        name: Service name (matches CREDENTIAL_TYPES key).
        fields: List of credential field names the user must provide.
        can_refresh_noninteractive: Whether credentials can be refreshed
            without user interaction. Always False for static providers.
    """

    name: str
    fields: list[str]
    env: dict[str, str] = msgspec.field(default_factory=dict)
    set_env: bool = False
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
        can_refresh_noninteractive=False,
    ),
    "linear": StaticProvider(
        name="linear",
        fields=["token"],
    ),
    "github": StaticProvider(
        name="github",
        fields=["token"],
        env={"token": "GH_TOKEN"},
        set_env=True,
    ),
    "aws": StaticProvider(
        name="aws",
        fields=["access_key_id", "secret_access_key", "session_token"],
        env={
            "access_key_id": "AWS_ACCESS_KEY_ID",
            "secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "session_token": "AWS_SESSION_TOKEN",
        },
        set_env=True,
    ),
    "scalr": StaticProvider(
        name="scalr",
        fields=["token", "hostname", "account"],
        env={
            "token": "SCALR_TOKEN",
            "hostname": "SCALR_HOSTNAME",
            "account": "SCALR_ACCOUNT",
        },
        set_env=True,
    ),
    "jira": StaticProvider(
        name="jira",
        fields=["email", "token", "cloud_id"],
    ),
    "notion": OAuthProvider(
        name="notion",
        server_url="https://mcp.notion.com",
    ),
    "slack": OAuthProvider(
        name="slack",
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        token_path="authed_user.access_token",
        refresh_token_path="authed_user.refresh_token",
        expires_in_path="authed_user.expires_in",
        extra_params={
            "user_scope": (
                "channels:history channels:read groups:history groups:read "
                "users:read search:read im:history mpim:history"
            ),
        },
    ),
    "google": OAuthProvider(
        name="google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        extra_params={"access_type": "offline", "prompt": "consent"},
    ),
}
