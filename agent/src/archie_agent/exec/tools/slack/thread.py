"""Bounded Slack thread context."""

from __future__ import annotations

import re

from archie_agent.exec.tools import tool

from .client import api_call
from .errors import SlackResponseError, SlackValidationError
from .messages import _parse, _project
from .policy import require_read
from .resolver import resolve_conversation_reference

_THREAD_TS = re.compile(r"^[0-9]{1,12}\.[0-9]{6}$")


@tool(native=False, exec_docs=False, namespace="slack")
async def thread(conversation: str, thread_ts: str, *, limit: int = 100) -> list[dict]:
    """Read a Slack thread with its parent first."""
    if not isinstance(conversation, str) or not conversation.strip():
        raise SlackValidationError("conversation is required")
    if (
        not isinstance(thread_ts, str)
        or not _THREAD_TS.fullmatch(thread_ts)
        or float(thread_ts) <= 0
    ):
        raise SlackValidationError("thread_ts must be a positive Slack timestamp")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SlackValidationError("limit must be 1-100")
    require_read()
    target = await resolve_conversation_reference(conversation)
    envelope = await api_call(
        "conversations.replies", {"channel": target["id"], "ts": thread_ts, "limit": limit}
    )
    if envelope.get("ok") is not True or not isinstance(envelope.get("messages"), list):
        raise SlackResponseError("Slack returned malformed thread data")
    values = []
    seen: set[str] = set()
    for item in envelope["messages"]:
        if not isinstance(item, dict) or not isinstance(item.get("ts"), str):
            raise SlackResponseError("Slack returned malformed thread data")
        timestamp, instant = _parse(item["ts"])
        if timestamp in seen:
            continue
        seen.add(timestamp)
        values.append((item["ts"] == thread_ts, instant, _project(item)))
    parents = [item for item in values if item[0]]
    if not parents:
        raise SlackResponseError("Slack thread parent was not found")
    parent = parents[0]
    replies = sorted((item for item in values if not item[0]), key=lambda item: item[1])
    return [parent[2], *(item[2] for item in replies[: max(0, limit - 1)])]
