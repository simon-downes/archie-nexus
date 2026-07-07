"""Tests for the refresh engine (refresh.py)."""

from datetime import UTC, datetime, timedelta

import pytest
from archie_shared.credentials.refresh import (
    InteractiveReauthRequired,
    can_refresh_noninteractive,
    is_expired,
)

# --- Tests: is_expired ---


def test_is_expired_none():
    """None expires_at → not expired (non-expiring)."""
    assert is_expired(None) is False


def test_is_expired_future():
    """Token expiring well in the future → not expired."""
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert is_expired(future) is False


def test_is_expired_past():
    """Token expired in the past → expired."""
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert is_expired(past) is True


def test_is_expired_within_skew():
    """Token expiring within skew window → treated as expired."""
    # 30 seconds from now, with default 60s skew → expired
    near_future = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    assert is_expired(near_future, skew=60) is True


def test_is_expired_outside_skew():
    """Token expiring beyond skew window → not expired."""
    # 90 seconds from now, with 60s skew → not expired
    near_future = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
    assert is_expired(near_future, skew=60) is False


def test_is_expired_unparseable():
    """Unparseable expires_at → treated as expired."""
    assert is_expired("not-a-date") is True
    assert is_expired("") is True


# --- Tests: can_refresh_noninteractive ---


def test_can_refresh_bedrock():
    """Bedrock cannot be refreshed non-interactively."""
    assert can_refresh_noninteractive("bedrock") is False


def test_can_refresh_static_providers():
    """Static providers cannot be refreshed."""
    for service in ["linear", "github", "aws", "scalr", "jira"]:
        assert can_refresh_noninteractive(service) is False


def test_can_refresh_oauth_providers():
    """OAuth providers can be refreshed."""
    for service in ["notion", "slack", "google"]:
        assert can_refresh_noninteractive(service) is True


def test_can_refresh_unknown():
    """Unknown service → cannot refresh."""
    assert can_refresh_noninteractive("unknown_service") is False


# --- Tests: InteractiveReauthRequired ---


def test_interactive_reauth_is_exception():
    """InteractiveReauthRequired is a proper exception."""
    with pytest.raises(InteractiveReauthRequired):
        raise InteractiveReauthRequired("test message")
