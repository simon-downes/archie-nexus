"""Read reactions from one known Slack message."""

from __future__ import annotations

import re
from typing import Any

from archie_agent.exec.tools import tool

from .client import api_call
from .errors import SlackResponseError, SlackValidationError
from .policy import require_read
from .resolver import resolve_conversation_reference

_TIMESTAMP = re.compile(r"^[0-9]{1,20}\.[0-9]{6}$")


@tool(native=False, exec_docs=False, namespace="slack")
async def reactions(conversation: str, timestamp: str) -> dict[str, Any]:
    """Read normalized reactions from one known Slack message."""
    if not isinstance(conversation, str) or not conversation.strip() or len(conversation) > 200:
        raise SlackValidationError("conversation must be 1-200 characters")
    if (
        not isinstance(timestamp, str)
        or not _TIMESTAMP.fullmatch(timestamp)
        or float(timestamp) <= 0
    ):
        raise SlackValidationError("timestamp must be a positive Slack timestamp")
    require_read()
    target = await resolve_conversation_reference(conversation)
    envelope = await api_call(
        "reactions.get",
        {"channel": target["id"], "timestamp": timestamp, "full": True},
    )
    if envelope.get("ok") is not True or not isinstance(envelope.get("message"), dict):
        raise SlackResponseError("Slack returned malformed reaction data")
    raw_reactions = envelope["message"].get("reactions", [])
    if not isinstance(raw_reactions, list):
        raise SlackResponseError("Slack returned malformed reaction data")
    result: list[dict[str, Any]] = []
    for reaction in raw_reactions:
        if not isinstance(reaction, dict):
            raise SlackResponseError("Slack returned malformed reaction data")
        name = reaction.get("name")
        count = reaction.get("count")
        users = reaction.get("users", [])
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 50
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(users, list)
            or any(not isinstance(user_id, str) or not user_id for user_id in users)
        ):
            raise SlackResponseError("Slack returned malformed reaction data")
        result.append({"name": name, "count": count, "users": users[:100]})
    return {"conversation": target["id"], "timestamp": timestamp, "reactions": result[:100]}
