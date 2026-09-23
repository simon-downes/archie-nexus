"""Slack message and reaction mutations."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from archie_agent.exec.tools import tool

from .client import api_call
from .errors import (
    SlackMutationIndeterminateError,
    SlackPolicyError,
    SlackResponseError,
    SlackValidationError,
)
from .policy import deny_conversation, policy
from .resolver import resolve_conversation_reference

_TS = re.compile(r"^[0-9]{1,20}\.[0-9]{6}$")
_REACTION = re.compile(r"^[A-Za-z0-9_+\-]+$")
_ARCHIE_PREFIX = ":archie: "


def _text_object(value: Any, *, allowed: set[str], max_length: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise SlackValidationError("Invalid Slack text object")
    kind = value.get("type")
    if kind not in {"plain_text", "mrkdwn"} or not isinstance(value.get("text"), str):
        raise SlackValidationError("Invalid Slack text object")
    if kind == "plain_text" and "verbatim" in value:
        raise SlackValidationError("Invalid Slack text object")
    if kind == "mrkdwn" and "emoji" in value:
        raise SlackValidationError("Invalid Slack text object")
    if not value["text"] or len(value["text"]) > max_length:
        raise SlackValidationError("Invalid Slack text")
    for key in set(value) & {"emoji", "verbatim"}:
        if not isinstance(value[key], bool):
            raise SlackValidationError("Invalid Slack text flag")
    return value


def validate_blocks(blocks: Any) -> list[dict[str, Any]] | None:
    if blocks is None:
        return None
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 50:
        raise SlackValidationError("blocks must contain 1-50 items")
    result: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise SlackValidationError("Invalid Slack block")
        kind = block["type"]
        if kind == "divider":
            if set(block) != {"type"}:
                raise SlackValidationError("Invalid divider block")
        elif kind == "header":
            if set(block) != {"type", "text"}:
                raise SlackValidationError("Invalid header block")
            text = block["text"]
            if not isinstance(text, dict) or text.get("type") != "plain_text":
                raise SlackValidationError("Invalid header block")
            _text_object(text, allowed={"type", "text", "emoji"}, max_length=150)
        elif kind == "section":
            if set(block) - {"type", "text", "fields"} or not (
                "text" in block or "fields" in block
            ):
                raise SlackValidationError("Invalid section block")
            if "text" in block:
                text = block["text"]
                if not isinstance(text, dict) or text.get("type") != "mrkdwn":
                    raise SlackValidationError("Invalid section text")
                _text_object(text, allowed={"type", "text", "verbatim"}, max_length=3000)
            if "fields" in block:
                fields = block["fields"]
                if not isinstance(fields, list) or not 1 <= len(fields) <= 10:
                    raise SlackValidationError("Invalid section fields")
                for field in fields:
                    if not isinstance(field, dict) or field.get("type") != "mrkdwn":
                        raise SlackValidationError("Invalid section field")
                    _text_object(field, allowed={"type", "text", "verbatim"}, max_length=3000)
        elif kind == "context":
            elements = block.get("elements")
            if (
                set(block) != {"type", "elements"}
                or not isinstance(elements, list)
                or not 1 <= len(elements) <= 10
            ):
                raise SlackValidationError("Invalid context block")
            for element in elements:
                _text_object(
                    element, allowed={"type", "text", "emoji", "verbatim"}, max_length=3000
                )
        else:
            raise SlackValidationError("Unsupported Slack block type")
        result.append(block)
    if len(json.dumps(result, separators=(",", ":"))) > 12000:
        raise SlackValidationError("Slack blocks are too large")
    return result


def _validate_target(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200 or value != value.strip():
        raise SlackValidationError("conversation must be a trimmed non-empty string")
    return value


@tool(native=False, exec_docs=False, namespace="slack")
async def send_message(
    conversation: str,
    text: str,
    *,
    thread_ts: str | None = None,
    blocks: list[dict] | None = None,
) -> dict:
    """Send Archie-prefixed text to an existing permitted Slack conversation."""
    _validate_target(conversation)
    if not isinstance(text, str) or not text.strip() or len(text) > 40000:
        raise SlackValidationError("text must be 1-40000 characters")
    if thread_ts is not None and (not isinstance(thread_ts, str) or not _TS.fullmatch(thread_ts)):
        raise SlackValidationError("thread_ts must be a Slack timestamp")
    validated_blocks = validate_blocks(blocks)
    active = policy()
    if not active["enabled"] or not active["write"]["enabled"]:
        raise SlackPolicyError("Slack writes are disabled by policy")
    target = await resolve_conversation_reference(conversation)
    if deny_conversation(target, active["write"]["scope"]["deny"]):
        raise SlackPolicyError("Slack conversation is denied by policy")
    payload = {"channel": target["id"], "text": _ARCHIE_PREFIX + text}
    if validated_blocks is not None:
        payload["blocks"] = validated_blocks
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    try:
        envelope = await api_call("chat.postMessage", payload)
    except SlackResponseError:
        raise
    except Exception as exc:
        raise SlackMutationIndeterminateError("Slack message send outcome is unknown") from exc
    if (
        envelope.get("ok") is not True
        or not isinstance(envelope.get("ts"), str)
        or not _TS.fullmatch(envelope["ts"])
    ):
        raise SlackMutationIndeterminateError("Slack message send outcome is unknown")
    permalink = envelope.get("permalink")
    if not isinstance(permalink, str) or not permalink.startswith("https://"):
        permalink = None
    return {
        "conversation_id": target["id"],
        "message_ts": envelope["ts"],
        "thread_ts": thread_ts,
        "permalink": permalink,
    }


@tool(native=False, exec_docs=False, namespace="slack")
async def react(
    conversation: str, timestamp: str, reaction: str, *, action: Literal["add", "remove"] = "add"
) -> dict[str, str]:
    """Add or remove one reaction on a known Slack message."""
    _validate_target(conversation)
    if not isinstance(timestamp, str) or not _TS.fullmatch(timestamp) or float(timestamp) <= 0:
        raise SlackValidationError("timestamp must be a positive Slack timestamp")
    if not isinstance(reaction, str) or not _REACTION.fullmatch(reaction) or len(reaction) > 50:
        raise SlackValidationError("reaction must be a bare emoji name")
    if not isinstance(action, str) or action not in ("add", "remove"):
        raise SlackValidationError("action must be add or remove")
    active = policy()
    if not active["enabled"] or not active["write"]["enabled"]:
        raise SlackPolicyError("Slack writes are disabled by policy")
    target = await resolve_conversation_reference(conversation)
    if deny_conversation(target, active["write"]["scope"]["deny"]):
        raise SlackPolicyError("Slack conversation is denied by policy")
    try:
        envelope = await api_call(
            f"reactions.{action}",
            {"channel": target["id"], "timestamp": timestamp, "name": reaction},
        )
    except SlackResponseError:
        raise
    except Exception as exc:
        raise SlackMutationIndeterminateError("Slack reaction outcome is unknown") from exc
    if envelope.get("ok") is not True:
        raise SlackMutationIndeterminateError("Slack reaction outcome is unknown")
    return {
        "conversation": target["id"],
        "timestamp": timestamp,
        "reaction": reaction,
        "action": action,
    }
