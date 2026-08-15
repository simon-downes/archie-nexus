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

from archie_shared.canonical_events import LLMRequest, decode_event

# --- Server → Client Events ---

PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class SessionInfo:
    """Sent to client immediately on WebSocket connect. No turn_index."""

    protocol_version: int
    model: str
    session_id: str
    git_branch: str = "—"

    def to_json(self) -> dict:
        return {
            "type": "session_info",
            "data": {
                "protocol_version": self.protocol_version,
                "model": self.model,
                "session_id": self.session_id,
                "git_branch": self.git_branch,
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> SessionInfo:
        return cls(
            protocol_version=data["protocol_version"],
            model=data["model"],
            session_id=data["session_id"],
            git_branch=data.get("git_branch", "—"),
        )


@dataclass(frozen=True)
class IterationStart:
    """Signals the start of a new tool-loop iteration.

    The client uses this to open a fresh visual block deterministically,
    independent of token-usage metadata. `index` is the 0-based iteration
    number within the current turn.
    """

    turn_index: int
    index: int

    def to_json(self) -> dict:
        return {
            "type": "iteration_start",
            "turn_index": self.turn_index,
            "data": {"index": self.index},
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> IterationStart:
        return cls(turn_index=turn_index, index=data.get("index", 0))


@dataclass(frozen=True)
class TextDelta:
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
    def from_json(cls, turn_index: int, data: dict) -> TextDelta:
        return cls(turn_index=turn_index, text=data["text"])


@dataclass(frozen=True)
class Usage:
    """Per-request token usage emitted after each LLM request.

    Token fields are for that single request only.
    context_pct is server-computed (session-level context window percentage).
    """

    turn_index: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    context_pct: float = 0.0

    def to_json(self) -> dict:
        return {
            "type": "usage",
            "turn_index": self.turn_index,
            "data": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "context_pct": self.context_pct,
            },
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> Usage:
        return cls(
            turn_index=turn_index,
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            cache_read_tokens=data["cache_read_tokens"],
            cache_write_tokens=data["cache_write_tokens"],
            context_pct=data.get("context_pct", 0.0),
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


@dataclass(frozen=True)
class ToolCall:
    """The model requested a tool call."""

    turn_index: int
    tool_use_id: str
    name: str
    input: dict

    def to_json(self) -> dict:
        return {
            "type": "tool_call",
            "turn_index": self.turn_index,
            "data": {
                "tool_use_id": self.tool_use_id,
                "name": self.name,
                "input": self.input,
            },
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> ToolCall:
        return cls(
            turn_index=turn_index,
            tool_use_id=data["tool_use_id"],
            name=data["name"],
            input=data["input"],
        )


@dataclass(frozen=True)
class ToolResult:
    """Result from tool execution."""

    turn_index: int
    tool_use_id: str
    is_error: bool
    content: str
    duration_ms: int = 0
    result_bytes: int = 0

    def to_json(self) -> dict:
        return {
            "type": "tool_result",
            "turn_index": self.turn_index,
            "data": {
                "tool_use_id": self.tool_use_id,
                "is_error": self.is_error,
                "content": self.content,
                "duration_ms": self.duration_ms,
                "result_bytes": self.result_bytes,
            },
        }

    @classmethod
    def from_json(cls, turn_index: int, data: dict) -> ToolResult:
        return cls(
            turn_index=turn_index,
            tool_use_id=data["tool_use_id"],
            is_error=data["is_error"],
            content=data["content"],
            duration_ms=data.get("duration_ms", 0),
            result_bytes=data.get("result_bytes", 0),
        )


@dataclass(frozen=True)
class ModelSwitched:
    """Server confirms model switch. No turn_index (session-level event)."""

    model_key: str
    model_name: str
    supports_cache: bool = False

    def to_json(self) -> dict:
        return {
            "type": "model_switched",
            "data": {
                "model_key": self.model_key,
                "model_name": self.model_name,
                "supports_cache": self.supports_cache,
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> ModelSwitched:
        return cls(
            model_key=data["model_key"],
            model_name=data["model_name"],
            supports_cache=data.get("supports_cache", False),
        )


@dataclass(frozen=True)
class StatusUpdated:
    """Post-turn status refresh. No turn_index (session-level event)."""

    git_branch: str

    def to_json(self) -> dict:
        return {
            "type": "status_updated",
            "data": {"git_branch": self.git_branch},
        }

    @classmethod
    def from_json(cls, data: dict) -> StatusUpdated:
        return cls(git_branch=data.get("git_branch", "—"))


@dataclass(frozen=True)
class SessionSnapshot:
    """Sent on WebSocket connect: authoritative session state for replay.

    Carries the id of the latest persisted canonical event so the client can
    request incremental replay via `/events?after=<latest_event_id>`, plus
    cumulative accounting derived from the persisted event log (authoritative
    regardless of agent process lifetime). No turn_index (session-level).
    """

    protocol_version: int
    model: str
    session_id: str
    latest_event_id: str | None = None
    status: str = "ready"
    git_branch: str = "—"
    total_cost: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0

    def to_json(self) -> dict:
        return {
            "type": "session_snapshot",
            "data": {
                "protocol_version": self.protocol_version,
                "model": self.model,
                "session_id": self.session_id,
                "latest_event_id": self.latest_event_id,
                "status": self.status,
                "git_branch": self.git_branch,
                "accounting": {
                    "total_cost": self.total_cost,
                    "total_input_tokens": self.total_input_tokens,
                    "total_output_tokens": self.total_output_tokens,
                    "total_cache_read_tokens": self.total_cache_read_tokens,
                    "total_cache_write_tokens": self.total_cache_write_tokens,
                },
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> SessionSnapshot:
        acct = data.get("accounting", {})
        return cls(
            protocol_version=data["protocol_version"],
            model=data["model"],
            session_id=data["session_id"],
            latest_event_id=data.get("latest_event_id"),
            status=data.get("status", "ready"),
            git_branch=data.get("git_branch", "—"),
            total_cost=acct.get("total_cost", 0.0),
            total_input_tokens=acct.get("total_input_tokens", 0),
            total_output_tokens=acct.get("total_output_tokens", 0),
            total_cache_read_tokens=acct.get("total_cache_read_tokens", 0),
            total_cache_write_tokens=acct.get("total_cache_write_tokens", 0),
        )


# Union of all server→client events
type ServerEvent = (
    SessionInfo
    | IterationStart
    | TextDelta
    | Usage
    | TurnComplete
    | TurnInterrupted
    | TurnError
    | ToolCall
    | ToolResult
    | ModelSwitched
    | StatusUpdated
    | SessionSnapshot
    | LLMRequest
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
    """Client requests turn or targeted child cancellation."""

    target: tuple[str, int] | None = None

    def to_json(self) -> dict:
        data: dict = {}
        if self.target is not None:
            data["scope"] = self.target[0]
            data["subagent_index"] = self.target[1]
        return {"type": "interrupt", "data": data}

    @classmethod
    def from_json(cls, data: dict) -> InterruptCommand:
        scope = data.get("scope")
        index = data.get("subagent_index")
        if scope is None and index is None:
            return cls()
        if not isinstance(scope, str) or not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("interrupt target requires scope and integer subagent_index")
        return cls(target=(scope, index))


@dataclass(frozen=True)
class SwitchModelCommand:
    """Client requests a model switch."""

    model_key: str  # catalog key, e.g. "bedrock-claude-haiku-4-5"

    def to_json(self) -> dict:
        return {
            "type": "switch_model",
            "data": {"model_key": self.model_key},
        }

    @classmethod
    def from_json(cls, data: dict) -> SwitchModelCommand:
        return cls(model_key=data["model_key"])


# Union of all client→server commands
type ClientCommand = MessageCommand | InterruptCommand | SwitchModelCommand


# --- Serialization helpers ---

_SERVER_EVENT_TYPES: dict[str, type] = {
    "session_info": SessionInfo,
    "iteration_start": IterationStart,
    "text_delta": TextDelta,
    "usage": Usage,
    "turn_complete": TurnComplete,
    "turn_interrupted": TurnInterrupted,
    "turn_error": TurnError,
    "tool_call": ToolCall,
    "tool_result": ToolResult,
    "model_switched": ModelSwitched,
    "status_updated": StatusUpdated,
    "session_snapshot": SessionSnapshot,
    "llm_request": LLMRequest,
}

_CLIENT_COMMAND_TYPES: dict[str, type] = {
    "message": MessageCommand,
    "interrupt": InterruptCommand,
    "switch_model": SwitchModelCommand,
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
    if cls is LLMRequest:
        # Broadcast raw as canonical JSON (no wire envelope) for the ledger/metrics
        # pipeline; the TUI consumes it for authoritative live cost accounting.
        return decode_event(raw)
    if cls in (SessionInfo, ModelSwitched, StatusUpdated, SessionSnapshot):
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
