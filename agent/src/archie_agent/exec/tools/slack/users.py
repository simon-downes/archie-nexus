"""Slack user directory and normalized identity lookup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from archie_agent.exec.tools import tool

from .cache import MetadataCache
from .client import api_call
from .errors import SlackResponseError, SlackValidationError
from .policy import require_read


def normalize_user(record: dict[str, Any]) -> dict[str, Any]:
    identifier = record.get("id")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
        raise SlackResponseError("Slack returned malformed user data")
    profile = record.get("profile") if isinstance(record.get("profile"), dict) else {}

    def text(name: str) -> str | None:
        value = record.get(name) or profile.get(name)
        return value.strip()[:256] if isinstance(value, str) and value.strip() else None

    flags = (record.get("is_bot", False), record.get("is_app_user", False), record.get("bot_id"))
    if not all(value is None or isinstance(value, (bool, str)) for value in flags):
        raise SlackResponseError("Slack returned malformed user data")
    return {
        "id": identifier,
        "username": text("name"),
        "real_name": text("real_name"),
        "display_name": text("display_name"),
        "email": text("email"),
        "deleted": record.get("deleted") is True,
        "is_bot": any(value is True or (isinstance(value, str) and bool(value)) for value in flags),
    }


async def _enumerate() -> list[dict]:
    values: list[dict] = []
    cursor: str | None = None
    for _ in range(100):
        payload = {"limit": 200}
        if cursor:
            payload["cursor"] = cursor
        envelope = await api_call("users.list", payload)
        if envelope.get("ok") is not True or not isinstance(envelope.get("members"), list):
            raise SlackResponseError("Slack returned malformed user data")
        values.extend(
            normalize_user(item) for item in envelope["members"] if isinstance(item, dict)
        )
        metadata = envelope.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise SlackResponseError("Slack returned malformed user pagination")
        cursor = metadata.get("next_cursor") or None
        if not cursor:
            return values
    raise SlackResponseError("Slack user pagination did not complete")


@tool(native=False, exec_docs=False, namespace="slack")
async def users(
    query: str | None = None, *, refresh: bool = False, include_deleted: bool = False
) -> list[dict]:
    """Find normalized, non-bot Slack users."""
    if query is not None and (not isinstance(query, str) or len(query) > 200):
        raise SlackValidationError("query must be at most 200 characters")
    if not isinstance(refresh, bool) or not isinstance(include_deleted, bool):
        raise SlackValidationError("refresh and include_deleted must be booleans")
    require_read()
    cache = MetadataCache(
        Path(os.environ["ARCHIE_HOME_DIR"]) if os.environ.get("ARCHIE_HOME_DIR") else None
    )
    records = None if refresh else cache.load("users", ttl=24 * 60 * 60)
    if records is None:
        records = await _enumerate()
        cache.save("users", records, ttl=24 * 60 * 60)
    needle = query.strip().casefold() if query else ""
    result = []
    for record in records:
        if record["is_bot"] or (record["deleted"] and not include_deleted):
            continue
        searchable = " ".join(
            str(record.get(key) or "")
            for key in ("id", "username", "real_name", "display_name", "email")
        )
        if needle and needle not in searchable.casefold():
            continue
        result.append(record)
    result.sort(
        key=lambda item: (
            item["display_name"] or "",
            item["real_name"] or "",
            item["username"] or "",
            item["id"],
        )
    )
    return result[:500]


async def lookup_user_ids(*, ids: list[str]) -> dict[str, dict | None]:
    """Resolve user IDs through one bounded users-list snapshot."""
    records = await _enumerate()
    by_id = {record["id"]: record for record in records}
    return {identifier: by_id.get(identifier) for identifier in ids}
