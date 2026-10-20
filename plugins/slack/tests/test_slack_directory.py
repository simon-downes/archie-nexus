"""Slack user and conversation directory tests (plugin cache under the context home, D14)."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.plugin import context, install_context
from archie_plugins.slack.conversations import conversations
from archie_plugins.slack.users import users
from slack_fakes import slack_context

_USERS = {
    "ok": True,
    "members": [
        {"id": "U1", "name": " Ada ", "real_name": "Ada", "profile": {"display_name": "ada"}}
    ],
}
_CONVERSATIONS = {
    "ok": True,
    "channels": [{"id": "C1", "name": "general", "is_channel": True, "is_archived": False}],
}


class Clock:
    """A settable clock shared by the context and its cache."""

    def __init__(self):
        self.now = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock(tmp_path):
    """Install a Slack context whose cache (``tmp_path/cache/slack``) uses a settable clock."""
    value = Clock()
    install_context(slack_context(tmp_path, clock=value))
    return value


async def test_users_normalize_and_cache(clock, tmp_path):
    with patch("archie_plugins.slack.users.api_call", new=AsyncMock(return_value=_USERS)) as call:
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


async def test_users_cache_file_ttl_refresh_and_expiry(clock, tmp_path):
    path = tmp_path / "cache" / "slack" / "slack.users.json"
    with patch("archie_plugins.slack.users.api_call", new=AsyncMock(return_value=_USERS)) as call:
        first = await users()
        assert call.await_count == 1
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert set(stored) == {"expires", "data"}
        assert set(stored["data"]) == {"records"}
        assert [record["id"] for record in stored["data"]["records"]] == ["U1"]

        clock.now += timedelta(hours=23)
        assert await users() == first
        assert call.await_count == 1  # within the 24h TTL: no API call

        assert await users(refresh=True) == first
        assert call.await_count == 2  # refresh always refetches

        clock.now += timedelta(hours=24, seconds=1)
        assert await users() == first
        assert call.await_count == 3  # expired: refetched


async def test_users_ignore_cache_entry_without_record_list(clock):
    context().cache.set("slack.users", {"records": "not-a-list"}, 60)
    with patch("archie_plugins.slack.users.api_call", new=AsyncMock(return_value=_USERS)) as call:
        result = await users()
    assert [record["id"] for record in result] == ["U1"]
    assert call.await_count == 1


async def test_users_over_cache_limit_are_returned_but_not_cached(tmp_path, monkeypatch):
    install_context(slack_context(tmp_path))
    monkeypatch.setattr(context().cache, "max_bytes", 10)
    with patch("archie_plugins.slack.users.api_call", new=AsyncMock(return_value=_USERS)) as call:
        first = await users()
        second = await users()
    assert [record["id"] for record in first] == ["U1"]
    assert second == first
    assert call.await_count == 2  # nothing was cached
    assert not (tmp_path / "cache" / "slack" / "slack.users.json").exists()


async def test_conversations_cache_and_projection(clock, tmp_path):
    with patch(
        "archie_plugins.slack.conversations.api_call",
        new=AsyncMock(return_value=_CONVERSATIONS),
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
    stored = json.loads((tmp_path / "cache" / "slack" / "slack.conversations.json").read_text())
    assert [record["id"] for record in stored["data"]["records"]] == ["C1"]


async def test_conversations_cache_expires_after_fifteen_minutes(clock):
    with patch(
        "archie_plugins.slack.conversations.api_call",
        new=AsyncMock(return_value=_CONVERSATIONS),
    ) as call:
        await conversations()
        clock.now += timedelta(minutes=14)
        await conversations()
        assert call.await_count == 1
        clock.now += timedelta(minutes=1, seconds=1)
        await conversations()
        assert call.await_count == 2


def test_manifest_cache_limit_fits_the_real_directory_sizes(tmp_path):
    # The live users list is ~133 KB (817 records): over the 256 KB default's comfort margin.
    assert slack_context(tmp_path).cache.max_bytes == 1048576
