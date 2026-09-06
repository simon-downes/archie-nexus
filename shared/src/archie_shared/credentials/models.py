"""Credential model structs — one per service.

All fields are Optional (default None) so partially-populated entries load
without error. forbid_unknown_fields=True catches typos/stale fields.
"""

import msgspec


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


class LinearCredential(msgspec.Struct, forbid_unknown_fields=True):
    """Linear API token."""

    token: str | None = None


class GithubCredential(msgspec.Struct, forbid_unknown_fields=True):
    """GitHub personal access token."""

    token: str | None = None


class ScalrCredential(msgspec.Struct, forbid_unknown_fields=True):
    """Scalr API token, hostname, and account."""

    token: str | None = None
    hostname: str | None = None
    account: str | None = None


class JiraCredential(msgspec.Struct, forbid_unknown_fields=True):
    """Jira Cloud credentials."""

    email: str | None = None
    token: str | None = None
    cloud_id: str | None = None


class AwsCredential(msgspec.Struct, forbid_unknown_fields=True):
    """Generic AWS credentials (for non-Bedrock AWS services)."""

    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None


# Registry: service name → credential struct type
CREDENTIAL_TYPES: dict[str, type] = {
    "bedrock": BedrockCredential,
    "notion": OAuthCredential,
    "slack": OAuthCredential,
    "google": OAuthCredential,
    "linear": LinearCredential,
    "github": GithubCredential,
    "scalr": ScalrCredential,
    "jira": JiraCredential,
    "aws": AwsCredential,
}
