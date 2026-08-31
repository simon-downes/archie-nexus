"""Client command protocol for the canonical session event stream."""

from __future__ import annotations

import json
from dataclasses import dataclass

# The canonical event schema is the only server-to-client protocol.
PROTOCOL_VERSION = 2


@dataclass(frozen=True)
class MessageCommand:
    """Client sends a user message."""

    content: str

    def to_json(self) -> dict:
        return {"type": "message", "data": {"content": self.content}}

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

    model_key: str

    def to_json(self) -> dict:
        return {"type": "switch_model", "data": {"model_key": self.model_key}}

    @classmethod
    def from_json(cls, data: dict) -> SwitchModelCommand:
        return cls(model_key=data["model_key"])


type ClientCommand = MessageCommand | InterruptCommand | SwitchModelCommand

_CLIENT_COMMAND_TYPES: dict[str, type] = {
    "message": MessageCommand,
    "interrupt": InterruptCommand,
    "switch_model": SwitchModelCommand,
}


def serialize_command(command: ClientCommand) -> str:
    """Serialize one client command."""
    return json.dumps(command.to_json())


def deserialize_command(raw: str) -> ClientCommand:
    """Deserialize one client command."""
    msg = json.loads(raw)
    cmd_type = msg["type"]
    cls = _CLIENT_COMMAND_TYPES.get(cmd_type)
    if cls is None:
        raise ValueError(f"Unknown command type: {cmd_type}")
    return cls.from_json(msg["data"])
