"""Conversation resolution seam for Slack direct targets."""

from __future__ import annotations

import re
from typing import Any

from .errors import SlackNotFoundError, SlackValidationError

_CONVERSATION_ID = re.compile(r"^[CGD][A-Za-z0-9]+$")


async def resolve_conversation_reference(reference: str, *, context: Any = None) -> dict[str, Any]:
    """Resolve a canonical Slack conversation ID without broad search syntax.

    Metadata-backed name and DM resolution is intentionally injectable; the default
    boundary accepts only canonical IDs until the shared cache resolver is supplied.
    """
    if not isinstance(reference, str) or not reference.strip():
        raise SlackValidationError("conversation must be a non-empty reference")
    value = reference.strip()
    if not _CONVERSATION_ID.fullmatch(value):
        raise SlackNotFoundError("Slack conversation was not found")
    kind = {"C": "channel", "G": "group_dm", "D": "dm"}[value[0].upper()]
    return {"id": value, "name": None, "type": kind}
