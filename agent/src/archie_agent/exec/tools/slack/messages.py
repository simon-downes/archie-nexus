"""Bounded Slack conversation history."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from archie_agent.exec.tools import tool

from .client import api_call
from .errors import SlackResponseError, SlackValidationError
from .policy import require_read
from .resolver import resolve_conversation_reference

_TS = re.compile(r"^[0-9]+\.[0-9]{1,6}$")


def _parse(value: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not _TS.fullmatch(value):
        raise SlackResponseError("Slack returned malformed timestamp")
    whole, fraction = value.split(".")
    instant = datetime.fromtimestamp(int(whole), UTC).replace(
        microsecond=int(fraction.ljust(6, "0"))
    )
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z"), instant


def _project(record: dict) -> dict:
    timestamp, _ = _parse(record["ts"])
    text = record.get("text")
    if not isinstance(text, str):
        raise SlackResponseError("Slack returned malformed message data")
    author_id = record.get("user") if isinstance(record.get("user"), str) else None
    return {
        "id": record.get("client_msg_id") or record.get("ts"),
        "type": record.get("subtype") or "message",
        "timestamp": timestamp,
        "author": {
            "id": author_id,
            "username": None,
            "real_name": None,
            "display_name": None,
            "is_deleted": None,
            "is_bot": None,
        },
        "text": text,
        "thread_timestamp": record.get("thread_ts"),
        "reply_count": record.get("reply_count")
        if isinstance(record.get("reply_count"), int) and record.get("reply_count") >= 0
        else None,
        "permalink": record.get("permalink") if isinstance(record.get("permalink"), str) else None,
    }


@tool(native=False, exec_docs=False, namespace="slack")
async def messages(
    conversation: str, *, after: str | None = None, before: str | None = None, limit: int = 50
) -> list[dict]:
    """Read bounded newest-first Slack conversation history."""
    if (
        not isinstance(conversation, str)
        or not conversation.strip()
        or len(conversation.strip()) > 200
    ):
        raise SlackValidationError("conversation must be 1-200 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SlackValidationError("limit must be 1-100")
    if after is not None and (not isinstance(after, str) or not _TS.fullmatch(after)):
        raise SlackValidationError("after must be a Slack timestamp")
    if before is not None and (not isinstance(before, str) or not _TS.fullmatch(before)):
        raise SlackValidationError("before must be a Slack timestamp")
    after_instant = _parse(after)[1] if after is not None else None
    before_instant = _parse(before)[1] if before is not None else None
    if after_instant and before_instant and after_instant >= before_instant:
        raise SlackValidationError("after must be before before")
    require_read()
    target = await resolve_conversation_reference(conversation)
    payload = {"channel": target["id"], "limit": limit}
    if after is not None:
        payload["oldest"] = after
    if before is not None:
        payload["latest"] = before
    envelope = await api_call("conversations.history", payload)
    if envelope.get("ok") is not True or not isinstance(envelope.get("messages"), list):
        raise SlackResponseError("Slack returned malformed message history")
    result = []
    seen: set[tuple[str, str]] = set()
    for record in envelope["messages"]:
        if not isinstance(record, dict):
            raise SlackResponseError("Slack returned malformed message history")
        projected = _project(record)
        instant = _parse(record["ts"])[1]
        if after_instant and instant <= after_instant:
            continue
        if before_instant and instant >= before_instant:
            continue
        key = (projected["timestamp"], str(projected["id"]))
        if key not in seen:
            seen.add(key)
            result.append((instant, projected))
    result.sort(key=lambda item: (item[0], str(item[1]["id"])), reverse=True)
    return [item[1] for item in result[:limit]]
