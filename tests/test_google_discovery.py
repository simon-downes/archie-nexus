from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools import get_all_tools
from archie_agent.exec.tools.discovery import search
from archie_agent.exec.tools.google_common import GoogleAuthorizationError, GoogleValidationError
from archie_shared.tool_policy import reset_policy_snapshot, set_policy_snapshot


async def test_discovery_is_registered_exactly():
    tools = get_all_tools()
    assert "google.search" in tools
    assert "discovery.search" not in tools
    assert tools["google.search"]._native is False
    assert tools["google.search"]._exec_docs is False


async def test_discovery_validates_before_policy_or_credentials():
    with patch("archie_agent.exec.tools.discovery.credential") as creds:
        with pytest.raises(GoogleValidationError):
            await search(" ")
    creds.assert_not_called()


async def test_discovery_denied_before_credentials():
    token = set_policy_snapshot({"google": {"read": {"enabled": False}}})
    try:
        with patch("archie_agent.exec.tools.discovery.credential") as creds:
            with pytest.raises(GoogleAuthorizationError):
                await search("roadmap", limit=20, types=["file"])
        creds.assert_not_called()
    finally:
        reset_policy_snapshot(token)


async def test_discovery_normalizes_and_deduplicates(monkeypatch):
    monkeypatch.setattr(
        "archie_agent.exec.tools.discovery.credential",
        lambda: type("C", (), {"access_token": "secret"})(),
    )
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
    monkeypatch.setattr("archie_agent.exec.tools.discovery.search_sources", provider)
    result = await search("plan", types=["file", "email"], limit=2)
    assert [item["id"] for item in result] == ["f1", "m1"]
    assert "secret" not in result[0]
    assert result[0]["handoff"] == "drive.metadata"
    provider.assert_awaited_once_with("plan", ["file", "email"], 2, "secret")
