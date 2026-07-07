"""Credential refresh engine — expiry checks and refresh orchestration.

This module is imported by the agent container, so it must NOT import
oauth.py at module level (httpx is CLI-only). OAuth imports are lazy,
inside the functions that need them.
"""

from datetime import UTC, datetime, timedelta

from archie_shared.credentials.providers import PROVIDERS, OAuthProvider


class InteractiveReauthRequired(Exception):  # noqa: N818
    """Raised when a credential cannot be refreshed non-interactively.

    Callers should surface an actionable error message directing the user
    to re-authenticate on the host.
    """


def is_expired(expires_at: str | None, skew: int = 60) -> bool:
    """Check if a credential has expired (with clock skew tolerance).

    Args:
        expires_at: ISO-UTC datetime string, or None (never expires).
        skew: Seconds of tolerance before actual expiry.

    Returns:
        True if expired or within skew seconds of expiry.
        False if expires_at is None (non-expiring credential).
    """
    if expires_at is None:
        return False

    try:
        expiry = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        # Unparseable expiry → treat as expired (force refresh)
        return True

    now = datetime.now(UTC)
    # Only add UTC if the parsed datetime is naive; preserve existing tzinfo
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return now >= expiry - timedelta(seconds=skew)


def can_refresh_noninteractive(service: str) -> bool:
    """Check if a service's credential can be refreshed without user interaction.

    Returns:
        True for OAuth services that may have a refresh_token.
        False for static providers and bedrock (requires interactive host auth).
    """
    provider = PROVIDERS.get(service)
    if provider is None:
        return False
    return provider.can_refresh_noninteractive


def refresh_credential(service: str) -> None:
    """Attempt non-interactive credential refresh for a service.

    For OAuth services: uses the stored refresh_token to obtain new tokens.
    For static/bedrock services: raises InteractiveReauthRequired.

    Raises:
        InteractiveReauthRequired: If the service cannot be refreshed
            non-interactively (user must re-authenticate on host).
        ValueError: If required refresh state is missing.
    """
    if not can_refresh_noninteractive(service):
        raise InteractiveReauthRequired(
            f"'{service}' credentials cannot be refreshed non-interactively. "
            f"Run 'archie auth login {service}' on the host to re-authenticate."
        )

    provider = PROVIDERS.get(service)
    if not isinstance(provider, OAuthProvider):
        raise InteractiveReauthRequired(f"'{service}' is not an OAuth provider — cannot refresh.")

    # Lazy import of oauth to avoid httpx dependency at module level
    from archie_shared.credentials.oauth import refresh_token as oauth_refresh
    from archie_shared.credentials.store import get_credential, set_credential

    cred = get_credential(service)
    if cred is None:
        raise ValueError(f"No stored credentials for '{service}'")

    if not hasattr(cred, "refresh_token") or cred.refresh_token is None:
        raise InteractiveReauthRequired(
            f"No refresh token for '{service}'. Run 'archie auth login {service}' on the host."
        )

    # Resolve token endpoint and client_id
    token_endpoint = getattr(cred, "token_endpoint", None) or provider.token_endpoint
    client_id = getattr(cred, "client_id", None)
    client_secret = getattr(cred, "client_secret", None)

    if not token_endpoint:
        raise ValueError(f"No token_endpoint configured for '{service}'")
    if not client_id:
        raise ValueError(f"No client_id stored for '{service}'")

    # Perform the refresh
    tokens = oauth_refresh(
        token_endpoint, client_id, cred.refresh_token, client_secret=client_secret
    )

    # Store updated tokens
    fields: dict[str, str | None] = {}
    access = extract_nested(tokens, provider.token_path)
    if access:
        fields["access_token"] = access

    refresh = extract_nested(tokens, provider.refresh_token_path)
    if refresh:
        fields["refresh_token"] = refresh

    expires_in = tokens.get("expires_in")
    if expires_in:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        fields["expires_at"] = expires_at.isoformat()

    if fields:
        set_credential(service, fields)


def extract_nested(data: dict, path: str) -> str | None:
    """Extract a value from a nested dict using dot-separated path.

    Args:
        data: Source dictionary.
        path: Dot-separated key path (e.g. "authed_user.access_token").

    Returns:
        String value at the path, or None if any intermediate key is missing.
    """
    for key in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
        if data is None:
            return None
    return str(data) if data is not None else None
