"""Session log persistence — canonical JSONL schema and append writer.

Each session produces one JSONL file at <ARCHIE_HOME_DIR>/sessions/{id}.jsonl.
Each line is one user exchange (prompt → response), encoded as a SessionLogEntry.

v1 is write-only (agent writes; nothing reads programmatically yet).
Files are consumed directly off the host mount by editors/models.
"""

from pathlib import Path

import msgspec


class ToolCall(msgspec.Struct):
    """A tool invocation within a turn (placeholder for v1 — tools not yet used)."""

    name: str
    source: str = ""
    result: str = ""


class EntryMetadata(msgspec.Struct, forbid_unknown_fields=True):
    """Per-exchange metadata written alongside the conversation content."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False
    backend: str | None = None


class SessionLogEntry(msgspec.Struct):
    """One JSONL line — a complete user exchange.

    Attributes:
        id: Unique entry ID (ULID string).
        when: ISO-8601 UTC timestamp of the exchange.
        user: The user's message text.
        assistant: The assistant's response text (None if interrupted before output).
        tools: Tool calls made during the exchange (empty in v1).
        metadata: Token usage, cost, and model info.
    """

    id: str
    when: str
    user: str
    metadata: EntryMetadata
    assistant: str | None = None
    tools: list[ToolCall] = msgspec.field(default_factory=list)


def write_entry(path: Path, entry: SessionLogEntry) -> None:
    """Append a single log entry as one JSONL line.

    Creates the parent directory on first write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    line = msgspec.json.encode(entry).decode()

    with path.open("a") as f:
        f.write(line + "\n")
