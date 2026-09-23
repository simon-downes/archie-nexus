"""Normalized, bounded Slack identity, conversation, and message models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Message:
    id: str | None
    conversation_id: str
    ts: str
    author: dict[str, Any] | None
    text: str
    subtype: str | None
    thread_ts: str | None
    reply_count: int | None
    permalink: str | None


def bounded(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:maximum]


def safe_id(value: Any) -> str | None:
    return bounded(value, 256)


def normalize_timestamp(value: Any) -> tuple[str, datetime] | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = float(value)
        parsed = datetime.fromtimestamp(timestamp, UTC)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            parsed = parsed.astimezone(UTC)
        except (ValueError, TypeError, OverflowError):
            return None
    precise = parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return precise, parsed


def safe_permalink(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    return value


def author(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    identifier = safe_id(value.get("id"))
    if not identifier:
        return None
    profile = value.get("profile") if isinstance(value.get("profile"), dict) else {}
    return {
        "id": identifier,
        "username": bounded(value.get("username") or value.get("name"), 256),
        "real_name": bounded(value.get("real_name"), 256),
        "display_name": bounded(profile.get("display_name") or value.get("display_name"), 256),
        "is_bot": value.get("is_bot") if isinstance(value.get("is_bot"), bool) else None,
        "is_deleted": value.get("deleted") if isinstance(value.get("deleted"), bool) else None,
    }


def conversation(value: Any, fallback: dict[str, Any] | None = None) -> dict[str, Any] | None:
    source = value if isinstance(value, dict) else fallback
    if not isinstance(source, dict):
        return None
    identifier = safe_id(source.get("id") or source.get("channel_id"))
    if not identifier:
        return None
    kind = source.get("type")
    if kind not in ("channel", "dm", "group_dm"):
        kind = "dm" if source.get("is_im") else "group_dm" if source.get("is_mpim") else "channel"
    return {"id": identifier, "name": bounded(source.get("name"), 256), "type": kind}


def message_projection(value: dict[str, Any]) -> tuple[dict[str, Any], datetime] | None:
    timestamp = normalize_timestamp(value.get("ts") or value.get("timestamp"))
    identifier = safe_id(value.get("id") or value.get("ts") or value.get("timestamp"))
    target = conversation(value.get("conversation") or value.get("channel"), value)
    text = bounded(value.get("text"), 4000)
    if not timestamp or not identifier or not target or text is None:
        return None
    return {
        "id": identifier,
        "timestamp": timestamp[0],
        "author": author(value.get("author") or value.get("user")),
        "text": text,
        "permalink": safe_permalink(value.get("permalink")),
    }, timestamp[1]
