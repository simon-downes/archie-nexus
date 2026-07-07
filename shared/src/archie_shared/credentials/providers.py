"""Provider registry — code-defined service descriptors.

Defines how each service authenticates (static token vs OAuth) and the
configuration needed for OAuth flows (endpoints, scopes, etc.).
"""

import msgspec


class StaticProvider(msgspec.Struct):
    """A service that uses static credentials (tokens, API keys).

    Attributes:
        name: Service name (matches CREDENTIAL_TYPES key).
        fields: List of credential field names the user must provide.
        can_refresh_noninteractive: Whether credentials can be refreshed
            without user interaction. Always False for static providers.
    """

    name: str
    fields: list[str]
    can_refresh_noninteractive: bool = False


class OAuthProvider(msgspec.Struct):
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
    scopes: list[str] | None = None
    token_path: str = "access_token"
    refresh_token_path: str = "refresh_token"
    extra_params: dict[str, str] | None = None
    can_refresh_noninteractive: bool = True


# --- Provider registry ---

PROVIDERS: dict[str, StaticProvider | OAuthProvider] = {
    "bedrock": StaticProvider(
        name="bedrock",
        fields=["aws_access_key_id", "aws_secret_access_key", "aws_session_token"],
        can_refresh_noninteractive=False,
    ),
    "linear": StaticProvider(
        name="linear",
        fields=["token"],
    ),
    "github": StaticProvider(
        name="github",
        fields=["token"],
    ),
    "aws": StaticProvider(
        name="aws",
        fields=["access_key_id", "secret_access_key", "session_token"],
    ),
    "scalr": StaticProvider(
        name="scalr",
        fields=["token", "hostname"],
    ),
    "jira": StaticProvider(
        name="jira",
        fields=["email", "token", "cloud_id"],
    ),
    "notion": OAuthProvider(
        name="notion",
        server_url="https://api.notion.com",
    ),
    "slack": OAuthProvider(
        name="slack",
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        token_path="authed_user.access_token",
        refresh_token_path="authed_user.refresh_token",
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
