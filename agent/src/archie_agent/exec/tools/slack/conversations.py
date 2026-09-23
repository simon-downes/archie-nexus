"""Authenticated-user Slack conversation directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from archie_agent.exec.tools import tool

from .cache import MetadataCache
from .client import api_call
from .errors import SlackResponseError, SlackValidationError
from .policy import deny_conversation, require_read

_TYPES = ("channel", "dm", "group_dm")
_PROVIDER_TYPES = "public_channel,private_channel,im,mpim"


def _type(record: dict) -> str | None:
    if record.get("is_im") is True:
        return "dm"
    if record.get("is_mpim") is True:
        return "group_dm"
    if record.get("is_channel") is True or record.get("is_group") is True:
        return "channel"
    return None


async def _enumerate() -> list[dict]:
    records: list[dict] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(100):
        payload = {"exclude_archived": True, "limit": 200, "types": _PROVIDER_TYPES}
        if cursor:
            payload["cursor"] = cursor
        envelope = await api_call("users.conversations", payload)
        if envelope.get("ok") is not True or not isinstance(envelope.get("channels"), list):
            raise SlackResponseError("Slack returned malformed conversation data")
        for record in envelope["channels"]:
            if not isinstance(record, dict):
                raise SlackResponseError("Slack returned malformed conversation data")
            identifier = record.get("id")
            kind = _type(record)
            if not isinstance(identifier, str) or not identifier or kind is None:
                raise SlackResponseError("Slack returned malformed conversation data")
            if record.get("is_archived") is True:
                continue
            records.append(
                {
                    "id": identifier,
                    "type": kind,
                    "name": record.get("name") if isinstance(record.get("name"), str) else None,
                    "is_private": record.get("is_private")
                    if isinstance(record.get("is_private"), bool)
                    else None,
                    "is_muted": record.get("is_muted")
                    if isinstance(record.get("is_muted"), bool)
                    else None,
                    "is_archived": False,
                    "participants": (
                        [{"id": record["user"]}]
                        if kind == "dm" and isinstance(record.get("user"), str)
                        else []
                    ),
                }
            )
        metadata = envelope.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise SlackResponseError("Slack returned malformed conversation data")
        cursor = metadata.get("next_cursor") or None
        if cursor is not None and (not isinstance(cursor, str) or cursor in seen):
            raise SlackResponseError("Slack returned malformed conversation pagination")
        if not cursor:
            return records
        seen.add(cursor)
    raise SlackResponseError("Slack conversation pagination did not complete")


@tool(native=False, exec_docs=False, namespace="slack")
async def conversations(
    *,
    types: list[Literal["channel", "dm", "group_dm"]] | None = None,
    query: str | None = None,
    refresh: bool = False,
) -> list[dict]:
    """List the authenticated user's permitted Slack conversations."""
    if types is not None and (
        not isinstance(types, list)
        or not types
        or len(set(types)) != len(types)
        or any(item not in _TYPES for item in types)
    ):
        raise SlackValidationError("types must be a distinct supported list")
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 200):
        raise SlackValidationError("query must be 1-200 characters")
    if not isinstance(refresh, bool):
        raise SlackValidationError("refresh must be a boolean")
    active = require_read()
    cache = MetadataCache(
        Path(os.environ["ARCHIE_HOME_DIR"]) if os.environ.get("ARCHIE_HOME_DIR") else None
    )
    records = None if refresh else cache.load("conversations", ttl=15 * 60)
    if records is None:
        records = await _enumerate()
        cache.save("conversations", records, ttl=15 * 60)
    selected = types or list(_TYPES)
    needle = query.strip().casefold() if query else None
    result = []
    for record in records:
        if record["type"] not in selected or deny_conversation(
            record, active["read"]["scope"]["deny"]
        ):
            continue
        if (
            needle
            and needle
            not in " ".join(
                [record["id"], record["name"] or "", *(p["id"] for p in record["participants"])]
            ).casefold()
        ):
            continue
        result.append(record)
    return result[:500]
