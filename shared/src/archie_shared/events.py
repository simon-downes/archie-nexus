"""Wire protocol events and commands for client↔agent WebSocket communication.

Server → Client: events streamed during agent turns.
Client → Server: commands sent by the TUI/web client.

All events carry a `turn_index` (monotonic int, incremented per user message).
The user message and its assistant response share the same turn_index.
This enables client-side reconciliation when connecting mid-turn.

Serialization: each type has `to_json()` → dict and `from_json(data)` → instance.
The wire format is: {"type": "<event_type>", "turn_index": N, "data": {...}}
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# --- Server → Client Events ---

PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class SessionInfo:
    """Sent to client immediately on WebSocket connect. No turn_index."""

    protocol_version: int
    model: str
    session_id: str

    def to_json(self) -> dict:
        return {
            "type": "session_info",
            "data": {
                "protocol_version": self.protocol_version,
                "model": self.model,
                "session_id": self.session_id,
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> SessionInfo:
        return cls(
            protocol_version=data["protocol_version"],
            model=data["model"],
            session_id=data["session_id"],
        )


@dataclass(frozen=True)
class TextDeltaEvent:
    """A chunk of streamed assistant text."""

    turn_index: int
    text: str

    def to_json(self) -> dict:
        return {
            "type": "text_delta",
            "turn_index": self.turn_index,
            "data": {"text": self.text},
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> TextDeltaEvent:
        return cls(turn_index=turn_index, text=data["text"])


@dataclass(frozen=True)
class UsageUpdated:
    """Token usage snapshot emitted after each LLM request."""

    turn_index: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: float

    def to_json(self) -> dict:
        return {
            "type": "usage_updated",
            "turn_index": self.turn_index,
            "data": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "cost": self.cost,
            },
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> UsageUpdated:
        return cls(
            turn_index=turn_index,
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            cache_read_tokens=data["cache_read_tokens"],
            cache_write_tokens=data["cache_write_tokens"],
            cost=data["cost"],
        )


@dataclass(frozen=True)
class TurnComplete:
    """The turn ended normally."""

    turn_index: int
    stop_reason: str

    def to_json(self) -> dict:
        return {
            "type": "turn_complete",
            "turn_index": self.turn_index,
            "data": {"stop_reason": self.stop_reason},
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> TurnComplete:
        return cls(turn_index=turn_index, stop_reason=data["stop_reason"])


@dataclass(frozen=True)
class TurnInterrupted:
    """The turn was aborted by the user."""

    turn_index: int

    def to_json(self) -> dict:
        return {
            "type": "turn_interrupted",
            "turn_index": self.turn_index,
            "data": {},
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> TurnInterrupted:
        return cls(turn_index=turn_index)


@dataclass(frozen=True)
class TurnError:
    """A terminal failure for the current turn."""

    turn_index: int
    message: str

    def to_json(self) -> dict:
        return {
            "type": "turn_error",
            "turn_index": self.turn_index,
            "data": {"message": self.message},
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> TurnError:
        return cls(turn_index=turn_index, message=data["message"])


# Union of all server→client events
type ServerEvent = (
    SessionInfo | TextDeltaEvent | UsageUpdated | TurnComplete | TurnInterrupted | TurnError
)


# --- Client → Server Commands ---


@dataclass(frozen=True)
class MessageCommand:
    """Client sends a user message."""

    content: str

    def to_json(self) -> dict:
        return {
            "type": "message",
            "data": {"content": self.content},
        }

    @classmethod
    def from_json(cls, data: dict) -> MessageCommand:
        return cls(content=data["content"])


@dataclass(frozen=True)
class InterruptCommand:
    """Client requests turn cancellation."""

    def to_json(self) -> dict:
        return {
            "type": "interrupt",
            "data": {},
        }

    @classmethod
    def from_json(cls, data: dict) -> InterruptCommand:
        return cls()


# Union of all client→server commands
type ClientCommand = MessageCommand | InterruptCommand


# --- Serialization helpers ---

_SERVER_EVENT_TYPES: dict[str, type] = {
    "session_info": SessionInfo,
    "text_delta": TextDeltaEvent,
    "usage_updated": UsageUpdated,
    "turn_complete": TurnComplete,
    "turn_interrupted": TurnInterrupted,
    "turn_error": TurnError,
}

_CLIENT_COMMAND_TYPES: dict[str, type] = {
    "message": MessageCommand,
    "interrupt": InterruptCommand,
}


def serialize_event(event: ServerEvent) -> str:
    """Serialize a server event to a JSON string for WebSocket transmission."""
    return json.dumps(event.to_json())


def deserialize_event(raw: str) -> ServerEvent:
    """Deserialize a JSON string from WebSocket into a server event."""
    msg = json.loads(raw)
    event_type = msg["type"]
    cls = _SERVER_EVENT_TYPES.get(event_type)
    if cls is None:
        raise ValueError(f"Unknown event type: {event_type}")
    if cls is SessionInfo:
        return cls.from_json(msg["data"])
    return cls.from_json(msg.get("turn_index", 0), msg["data"])


def serialize_command(command: ClientCommand) -> str:
    """Serialize a client command to a JSON string for WebSocket transmission."""
    return json.dumps(command.to_json())


def deserialize_command(raw: str) -> ClientCommand:
    """Deserialize a JSON string from WebSocket into a client command."""
    msg = json.loads(raw)
    cmd_type = msg["type"]
    cls = _CLIENT_COMMAND_TYPES.get(cmd_type)
    if cls is None:
        raise ValueError(f"Unknown command type: {cmd_type}")
    return cls.from_json(msg["data"])
