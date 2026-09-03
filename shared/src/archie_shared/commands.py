"""Strict client-to-server command protocol."""

from __future__ import annotations

import msgspec


class MessageCommand(msgspec.Struct, tag="message", tag_field="type", forbid_unknown_fields=True):
    """Client sends a user message."""

    content: str


class InterruptCommand(
    msgspec.Struct,
    tag="interrupt",
    tag_field="type",
    forbid_unknown_fields=True,
    omit_defaults=True,
):
    """Client requests turn or targeted child cancellation."""

    scope: str | None = None
    subagent_index: int | None = None

    def __post_init__(self) -> None:
        if (self.scope is None) != (self.subagent_index is None):
            raise ValueError("interrupt target requires both scope and subagent_index")
        if isinstance(self.subagent_index, bool):
            raise TypeError("subagent_index must be an integer")

    @property
    def target(self) -> tuple[str, int] | None:
        """Return the legacy internal target shape for dispatch code."""
        if self.scope is None:
            return None
        assert self.subagent_index is not None
        return self.scope, self.subagent_index


class SwitchModelCommand(
    msgspec.Struct, tag="switch_model", tag_field="type", forbid_unknown_fields=True
):
    """Client requests a model switch."""

    model_key: str


type ClientCommand = MessageCommand | InterruptCommand | SwitchModelCommand


def encode_command(command: ClientCommand) -> str:
    """Encode one flat, tagged client command."""
    return msgspec.json.encode(command).decode("utf-8")


def decode_command(raw: str | bytes) -> ClientCommand:
    """Decode one strict flat client command."""
    return msgspec.json.decode(raw, type=ClientCommand)
