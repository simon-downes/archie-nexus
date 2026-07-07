"""Session log persistence — per-message JSONL schema and append writer.

Each session produces one JSONL file at <ARCHIE_HOME_DIR>/sessions/{id}.jsonl.
Each line is one message (user or assistant), encoded as a MessageEntry.

The user message is persisted before LLM streaming starts (crash-safe).
The assistant message is persisted after streaming completes.

v2 schema — replaces the per-exchange SessionLogEntry from v1.
Files are consumed directly off the host mount by editors/models.
"""

from pathlib import Path

import msgspec


class MessageMetadata(msgspec.Struct):
    """Per-message metadata for assistant entries.

    User entries have metadata=None. Assistant entries carry token usage,
    cost, and model info for that specific message.
    """

    model: str
    backend: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False


class MessageEntry(msgspec.Struct):
    """One JSONL line — a single message in the conversation.

    Attributes:
        id: Unique entry ID (ULID string).
        when: ISO-8601 UTC timestamp.
        role: Message role — "user" or "assistant" (later: "tool_result", "tool_call").
        content: Message text content.
        metadata: Token/cost metadata (None for user entries, populated for assistant).
    """

    id: str
    when: str
    role: str
    content: str
    metadata: MessageMetadata | None = None


def write_entry(path: Path, entry: MessageEntry) -> None:
    """Append a single message entry as one JSONL line.

    Creates the parent directory on first write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    line = msgspec.json.encode(entry).decode()

    with path.open("a") as f:
        f.write(line + "\n")
