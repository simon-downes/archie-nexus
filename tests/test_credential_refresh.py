from datetime import UTC, datetime, timedelta

import pytest
from archie_orchestrator.credential_refresh import refresh_expiring_credentials
from archie_shared.credentials.models import OAuthCredential


@pytest.mark.asyncio
async def test_refresh_sweep_refreshes_expiring_oauth_credentials(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    credentials = {
        "google": OAuthCredential(
            access_token="old", refresh_token="refresh", expires_at=(now + timedelta(minutes=2)).isoformat()
        )
    }
    refreshed = []

    monkeypatch.setattr(
        "archie_orchestrator.credential_refresh.get_credential",
        lambda name, credential_type: credentials.get(name),
    )

    class Service:
        async def refresh(self, name):
            refreshed.append(name)

    result = await refresh_expiring_credentials(Service(), now=now)

    assert result == ["google"]
    assert refreshed == ["google"]


@pytest.mark.asyncio
async def test_refresh_sweep_skips_missing_expiry_and_refresh_token(monkeypatch):
    credentials = {
        "google": OAuthCredential(access_token="token"),
        "slack": OAuthCredential(access_token="token", refresh_token="refresh"),
    }
    refreshed = []
    monkeypatch.setattr(
        "archie_orchestrator.credential_refresh.get_credential",
        lambda name, credential_type: credentials.get(name),
    )

    class Service:
        async def refresh(self, name):
            refreshed.append(name)

    result = await refresh_expiring_credentials(Service())

    assert result == []
    assert refreshed == []


@pytest.mark.asyncio
async def test_refresh_sweep_isolates_provider_failures(monkeypatch):
    now = datetime.now(UTC)
    credentials = {
        name: OAuthCredential(
            access_token="token",
            refresh_token="refresh",
            expires_at=(now + timedelta(seconds=1)).isoformat(),
        )
        for name in ("google", "slack")
    }
    refreshed = []
    monkeypatch.setattr(
        "archie_orchestrator.credential_refresh.get_credential",
        lambda name, credential_type: credentials.get(name),
    )

    class Service:
        async def refresh(self, name):
            if name == "google":
                raise RuntimeError("provider unavailable")
            refreshed.append(name)

    result = await refresh_expiring_credentials(Service(), now=now)

    assert result == ["slack"]
    assert refreshed == ["slack"]
