"""Periodic proactive refresh of stored OAuth credentials."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.credentials.providers import PROVIDERS, OAuthProvider
from archie_shared.credentials.store import get_credential

from archie_orchestrator.auth import AuthError, AuthService

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_REFRESH_WINDOW = timedelta(minutes=5)


def _expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        # Invalid expiry metadata should fail safe and trigger a refresh.
        return datetime.min.replace(tzinfo=UTC)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


async def refresh_expiring_credentials(
    auth_service: AuthService,
    *,
    now: datetime | None = None,
    refresh_window: timedelta = DEFAULT_REFRESH_WINDOW,
) -> list[str]:
    """Refresh OAuth credentials that expire within ``refresh_window``.

    A failed provider refresh does not prevent other providers from being
    checked, and the existing credential remains untouched by AuthService.
    """
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    refreshed: list[str] = []
    for name, provider in PROVIDERS.items():
        if not isinstance(provider, OAuthProvider):
            continue
        try:
            credential = get_credential(name, CREDENTIAL_TYPES[name])
        except (TypeError, ValueError) as exc:
            log.warning("Skipping refresh for %s: invalid stored credential (%s)", name, exc)
            continue
        if credential is None or not getattr(credential, "refresh_token", None):
            continue
        expires_at = _expiry(getattr(credential, "expires_at", None))
        if expires_at is None or expires_at > current_time + refresh_window:
            continue
        try:
            await auth_service.refresh(name)
        except AuthError as exc:
            log.warning("Could not refresh %s credentials: %s", name, exc)
        except Exception:
            log.exception("Unexpected error refreshing %s credentials", name)
        else:
            log.info("Refreshed %s credentials successfully", name)
            refreshed.append(name)
    return refreshed


async def credential_refresh_loop(
    auth_service: AuthService,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    refresh_window: timedelta = DEFAULT_REFRESH_WINDOW,
) -> None:
    """Run periodic OAuth credential refreshes until cancelled."""
    while True:
        await refresh_expiring_credentials(auth_service, refresh_window=refresh_window)
        await asyncio.sleep(interval_seconds)
