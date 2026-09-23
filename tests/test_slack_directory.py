"""Slack user and conversation directory tests."""

from unittest.mock import AsyncMock, patch

from archie_agent.exec.tools.slack.conversations import conversations
from archie_agent.exec.tools.slack.users import users


async def test_users_normalize_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    payload = {
        "ok": True,
        "members": [
            {"id": "U1", "name": " Ada ", "real_name": "Ada", "profile": {"display_name": "ada"}}
        ],
    }
    with patch(
        "archie_agent.exec.tools.slack.users.api_call", new=AsyncMock(return_value=payload)
    ) as call:
        first = await users(refresh=True)
        second = await users()
    assert first == second
    assert first[0]["id"] == "U1"
    assert set(first[0]) == {
        "id",
        "username",
        "real_name",
        "display_name",
        "email",
        "deleted",
        "is_bot",
    }
    assert call.await_count == 1


async def test_conversations_cache_and_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    payload = {
        "ok": True,
        "channels": [{"id": "C1", "name": "general", "is_channel": True, "is_archived": False}],
    }
    with patch(
        "archie_agent.exec.tools.slack.conversations.api_call", new=AsyncMock(return_value=payload)
    ) as call:
        result = await conversations(refresh=True)
        cached = await conversations()
    assert result == cached
    assert set(result[0]) == {
        "id",
        "type",
        "name",
        "is_private",
        "is_muted",
        "is_archived",
        "participants",
    }
    assert call.await_count == 1
