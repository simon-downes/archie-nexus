from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.plugin import install_context, plugin_tools
from archie_agent.exec.tools import get_all_tools
from archie_plugins.google.common import GoogleAuthorizationError, GoogleValidationError
from archie_plugins.google.discovery import search
from google_fakes import fake_credential, google_context


async def test_discovery_is_registered_exactly():
    tools = plugin_tools()
    assert "google.search" in tools
    assert "discovery.search" not in tools
    assert tools["google.search"]._plugin == "google"
    assert "google.search" not in get_all_tools()


async def test_discovery_validates_before_policy_or_credentials():
    with patch("archie_plugins.google.discovery.credential") as creds:
        with pytest.raises(GoogleValidationError):
            await search(" ")
    creds.assert_not_called()


async def test_discovery_denied_before_credentials(policy):
    policy({"google": {"read": {"enabled": False}}})
    with patch("archie_plugins.google.discovery.credential") as creds:
        with pytest.raises(GoogleAuthorizationError):
            await search("roadmap", limit=20, types=["file"])
    creds.assert_not_called()


async def test_discovery_normalizes_and_deduplicates(monkeypatch, google_context_installed):
    install_context(google_context(google_context_installed, credential=fake_credential("secret")))
    provider = AsyncMock(
        return_value=[
            {
                "type": "file",
                "id": "f1",
                "title": "Plan",
                "snippet": "body must not be here",
                "secret": "x",
            },
            {"type": "file", "id": "f1", "title": "duplicate"},
            {"type": "email", "id": "m1", "title": "Mail"},
        ]
    )
    monkeypatch.setattr("archie_plugins.google.discovery.search_sources", provider)
    result = await search("plan", types=["file", "email"], limit=2)
    assert [item["id"] for item in result] == ["f1", "m1"]
    assert "secret" not in result[0]
    assert result[0]["handoff"] == "drive.metadata"
    provider.assert_awaited_once_with("plan", ["file", "email"], 2, "secret")
