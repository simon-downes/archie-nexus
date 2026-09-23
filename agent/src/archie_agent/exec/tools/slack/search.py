"""Slack Real-time Search public boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from archie_agent.exec.tools import tool

from .client import search_context
from .errors import SlackValidationError
from .models import message_projection
from .policy import deny_conversation, require_read
from .resolver import resolve_conversation_reference

_CHANNEL_TYPES = ("public_channel", "private_channel", "im", "mpim")
_CONTENT_TYPES = ("messages", "files", "channels", "users")
_SCOPE_BY_CHANNEL = {
    "public_channel": "search:read.public",
    "private_channel": "search:read.private",
    "im": "search:read.im",
    "mpim": "search:read.mpim",
}


def required_scopes(content_types: list[str], channel_types: list[str]) -> list[str]:
    scopes = {_SCOPE_BY_CHANNEL[channel] for channel in channel_types}
    if "files" in content_types:
        scopes.add("search:read.files")
    if "users" in content_types:
        scopes.add("search:read.users")
    return sorted(scopes)


def _array(value: list[str] | None, allowed: tuple[str, ...], default: list[str]) -> list[str]:
    if value is None:
        return default.copy()
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise SlackValidationError("search options must be non-empty duplicate-free lists")
    if any(not isinstance(item, str) or item not in allowed for item in value):
        raise SlackValidationError("search options contain an unsupported value")
    return value.copy()


def _timestamp(value: str | None, name: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise SlackValidationError(f"{name} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SlackValidationError(f"{name} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SlackValidationError(f"{name} must be a timezone-aware ISO-8601 timestamp")
    instant = parsed.astimezone(UTC)
    return str(int(instant.timestamp())), instant


def build_rts_request(
    query: str,
    *,
    conversation: str | None = None,
    content_types: list[str] | None = None,
    channel_types: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    sort: Literal["relevance", "timestamp"] = "relevance",
    include_context: bool = True,
    limit: int = 20,
) -> tuple[dict, datetime | None, datetime | None]:
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > 1000:
        raise SlackValidationError("query must be 1-1000 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise SlackValidationError("limit must be 1-20")
    if sort not in ("relevance", "timestamp"):
        raise SlackValidationError("sort must be relevance or timestamp")
    if not isinstance(include_context, bool):
        raise SlackValidationError("include_context must be a boolean")
    if conversation is not None and (
        not isinstance(conversation, str)
        or not conversation.strip()
        or len(conversation.strip()) > 200
    ):
        raise SlackValidationError("conversation must be 1-200 characters")
    content = _array(content_types, _CONTENT_TYPES, ["messages"])
    channels = _array(channel_types, _CHANNEL_TYPES, list(_CHANNEL_TYPES))
    after_value, after_dt = _timestamp(after, "after")
    before_value, before_dt = _timestamp(before, "before")
    if after_dt and before_dt and after_dt >= before_dt:
        raise SlackValidationError("after must be before before")
    request = {
        "query": query.strip(),
        "channel_types": channels,
        "content_types": content,
        "limit": limit,
        "sort": "score" if sort == "relevance" else "timestamp",
        "sort_dir": "desc",
        "include_context_messages": include_context,
    }
    if after_value is not None:
        request["after"] = after_value
    if before_value is not None:
        request["before"] = before_value
    if conversation is not None:
        request["context_channel_id"] = conversation.strip()
    return request, after_dt, before_dt


@tool(native=False, exec_docs=False, namespace="slack")
async def search(
    query: str,
    *,
    conversation: str | None = None,
    content_types: list[str] | None = None,
    channel_types: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    sort: Literal["relevance", "timestamp"] = "relevance",
    include_context: bool = True,
    limit: int = 20,
) -> list[dict]:
    """Search Slack context through one bounded Real-time Search request."""
    request, after_instant, before_instant = build_rts_request(
        query,
        conversation=conversation,
        content_types=content_types,
        channel_types=channel_types,
        after=after,
        before=before,
        sort=sort,
        include_context=include_context,
        limit=limit,
    )
    active_policy = require_read()
    content = request["content_types"]
    channels = request["channel_types"]
    scopes = required_scopes(content, channels)
    if conversation is not None:
        target = await resolve_conversation_reference(conversation)
        if deny_conversation(target, active_policy["read"]["scope"]["deny"]):
            from .errors import SlackPolicyError

            raise SlackPolicyError("Slack conversation is denied by policy")
        request["context_channel_id"] = target["id"]
    envelope = await search_context(request, required_scopes=scopes)
    raw_results = envelope.get("results")
    if isinstance(raw_results, dict):
        raw_results = raw_results.get("messages")
    if envelope.get("ok") is not True or not isinstance(raw_results, list):
        from .errors import SlackResponseError

        raise SlackResponseError("Slack returned a malformed search response")
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "message" or "message_ts" in raw:
            if kind != "message":
                raw = {
                    **raw,
                    "id": raw.get("id") or raw.get("message_ts"),
                    "ts": raw.get("ts") or raw.get("message_ts"),
                    "text": raw.get("text") or raw.get("content"),
                    "user": raw.get("user") or raw.get("author_user_id"),
                    "channel_id": raw.get("channel_id"),
                    "conversation": {
                        "id": raw.get("channel_id"),
                        "name": raw.get("channel_name"),
                        "type": raw.get(
                            "conversation_type",
                            "dm" if str(raw.get("channel_id", "")).startswith("D") else "channel",
                        ),
                    },
                }
            normalized = message_projection(raw)
            if normalized is None:
                continue
            item, timestamp = normalized
            if after_instant and timestamp <= after_instant:
                continue
            if before_instant and timestamp >= before_instant:
                continue
            identifier = item["id"]
            target = raw.get("conversation") if isinstance(raw.get("conversation"), dict) else raw
            normalized_conversation = {
                "id": target.get("id") or target.get("channel_id"),
                "name": target.get("name") if isinstance(target.get("name"), str) else None,
                "type": target.get("type", "channel"),
            }
            if not isinstance(normalized_conversation["id"], str) or normalized_conversation[
                "type"
            ] not in ("channel", "dm", "group_dm"):
                continue
            if deny_conversation(normalized_conversation, active_policy["read"]["scope"]["deny"]):
                continue
            item = {
                "type": "message",
                "id": identifier,
                "conversation": normalized_conversation,
                "timestamp": item["timestamp"],
                "author": item["author"],
                "text": item["text"],
                "permalink": item["permalink"],
                "context": None,
            }
        elif kind in ("file", "channel", "user"):
            identifier = raw.get("id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
                continue
            text = (
                raw.get("title")
                or raw.get("name")
                or raw.get("display_name")
                or raw.get("real_name")
                or raw.get("username")
            )
            if not isinstance(text, str) or not text:
                continue
            item = {
                "type": kind,
                "id": identifier[:256],
                "conversation": None,
                "timestamp": None,
                "author": None,
                "text": text[: 512 if kind == "file" else 256],
                "permalink": None,
                "context": None,
            }
        else:
            continue
        key = (item["type"], item["id"])
        if key not in seen:
            seen.add(key)
            results.append(item)
    if sort == "timestamp":
        results.sort(key=lambda item: item["id"])
        results.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    results = results[:limit]
    while (
        len(json.dumps(results, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 12_000
        and results
    ):
        results.pop()
    return results
