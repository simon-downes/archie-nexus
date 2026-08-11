"""Tests for session log persistence (log.py) — per-message schema."""

import msgspec
from archie_shared.session.log import MessageEntry, MessageMetadata, write_entry


def test_write_user_entry(tmp_path):
    """User entries have metadata=None and create file + parent dirs."""
    path = tmp_path / "sessions" / "test.jsonl"
    entry = MessageEntry(
        id="01J3ABCDEF",
        when="2026-07-07T12:00:00+00:00",
        role="user",
        content="hello",
    )
    write_entry(path, entry)
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1

    decoded = msgspec.json.decode(lines[0], type=MessageEntry)
    assert decoded.role == "user"
    assert decoded.content == "hello"
    assert decoded.metadata is None


def test_write_assistant_entry(tmp_path):
    """Assistant entries carry full metadata."""
    path = tmp_path / "test.jsonl"
    entry = MessageEntry(
        id="01J3XYZ",
        when="2026-07-07T12:00:00+00:00",
        role="assistant",
        content="The answer is 4.",
        metadata=MessageMetadata(
            model="bedrock-claude-sonnet-4-6",
            backend="bedrock",
        ),
    )
    write_entry(path, entry)

    line = path.read_text().strip()
    decoded = msgspec.json.decode(line, type=MessageEntry)
    assert decoded.role == "assistant"
    assert decoded.content == "The answer is 4."
    assert decoded.metadata is not None
    assert decoded.metadata.model == "bedrock-claude-sonnet-4-6"
    assert decoded.metadata.backend == "bedrock"


def test_roundtrip_encode_decode(tmp_path):
    """Written entry can be decoded back to the same struct."""
    path = tmp_path / "test.jsonl"
    entry = MessageEntry(
        id="01J3RT",
        when="2026-07-07T12:00:00+00:00",
        role="assistant",
        content="hi",
        metadata=MessageMetadata(
            model="test-model",
            interrupted=True,
        ),
    )
    write_entry(path, entry)

    line = path.read_text().strip()
    decoded = msgspec.json.decode(line, type=MessageEntry)
    assert decoded == entry


def test_append_multiple(tmp_path):
    """Multiple writes append (one line per entry)."""
    path = tmp_path / "test.jsonl"
    for i in range(3):
        entry = MessageEntry(
            id=f"entry-{i}",
            when="2026-07-07T12:00:00+00:00",
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i}",
            metadata=MessageMetadata(model="test") if i % 2 == 1 else None,
        )
        write_entry(path, entry)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 3

    # Verify alternating roles
    entries = [msgspec.json.decode(line, type=MessageEntry) for line in lines]
    assert entries[0].role == "user"
    assert entries[1].role == "assistant"
    assert entries[2].role == "user"


def test_role_free_string():
    """Role is a free string — any value accepted (future tool_result etc.)."""
    entry = MessageEntry(
        id="01J3TOOL",
        when="2026-07-07T12:00:00+00:00",
        role="tool_result",
        content='{"output": "success"}',
    )
    encoded = msgspec.json.encode(entry)
    decoded = msgspec.json.decode(encoded, type=MessageEntry)
    assert decoded.role == "tool_result"


def test_interrupted_empty_content(tmp_path):
    """Interrupted assistant with empty content persists correctly."""
    path = tmp_path / "test.jsonl"
    entry = MessageEntry(
        id="01J3INT",
        when="2026-07-07T12:00:00+00:00",
        role="assistant",
        content="",
        metadata=MessageMetadata(model="test", interrupted=True),
    )
    write_entry(path, entry)

    line = path.read_text().strip()
    decoded = msgspec.json.decode(line, type=MessageEntry)
    assert decoded.content == ""
    assert decoded.metadata is not None
    assert decoded.metadata.interrupted is True
