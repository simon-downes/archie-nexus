"""Tests for session log persistence (log.py)."""

import msgspec
from archie_shared.session.log import EntryMetadata, SessionLogEntry, write_entry


def test_write_entry_creates_file(tmp_path):
    """write_entry creates the JSONL file and parent dirs."""
    path = tmp_path / "sessions" / "test.jsonl"
    entry = SessionLogEntry(
        id="01J3ABCDEF",
        when="2026-07-07T12:00:00+00:00",
        user="hello",
        assistant="hi there",
        metadata=EntryMetadata(
            model="bedrock-claude-sonnet-4-6", input_tokens=100, output_tokens=10
        ),
    )
    write_entry(path, entry)
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_write_entry_roundtrip(tmp_path):
    """Written entry can be decoded back to the same struct."""
    path = tmp_path / "test.jsonl"
    entry = SessionLogEntry(
        id="01J3XYZ",
        when="2026-07-07T12:00:00+00:00",
        user="what is 2+2?",
        assistant="4",
        metadata=EntryMetadata(
            model="bedrock-claude-sonnet-4-6",
            backend="bedrock",
            input_tokens=50,
            output_tokens=5,
            cache_read_tokens=20,
            cost=0.000123,
        ),
    )
    write_entry(path, entry)

    line = path.read_text().strip()
    decoded = msgspec.json.decode(line, type=SessionLogEntry)
    assert decoded.id == "01J3XYZ"
    assert decoded.user == "what is 2+2?"
    assert decoded.assistant == "4"
    assert decoded.metadata.backend == "bedrock"
    assert decoded.metadata.cost == 0.000123


def test_write_entry_assistant_none(tmp_path):
    """assistant=None is encoded (not omitted by msgspec default)."""
    path = tmp_path / "test.jsonl"
    entry = SessionLogEntry(
        id="01J3INT",
        when="2026-07-07T12:00:00+00:00",
        user="hi",
        metadata=EntryMetadata(model="test", interrupted=True),
    )
    write_entry(path, entry)

    line = path.read_text().strip()
    decoded = msgspec.json.decode(line, type=SessionLogEntry)
    assert decoded.assistant is None
    assert decoded.metadata.interrupted is True


def test_write_entry_append(tmp_path):
    """Multiple writes append (one line per entry)."""
    path = tmp_path / "test.jsonl"
    for i in range(3):
        entry = SessionLogEntry(
            id=f"entry-{i}",
            when="2026-07-07T12:00:00+00:00",
            user=f"msg {i}",
            metadata=EntryMetadata(model="test"),
        )
        write_entry(path, entry)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 3


def test_missing_backend_decodes():
    """Old log without 'backend' decodes correctly (field is optional)."""
    # Simulate an old log line without the backend field
    old_json = (
        '{"id":"old","when":"2026-01-01T00:00:00+00:00","user":"hi",'
        '"metadata":{"model":"test","input_tokens":10,"output_tokens":5}}'
    )
    decoded = msgspec.json.decode(old_json, type=SessionLogEntry)
    assert decoded.metadata.backend is None
    assert decoded.metadata.model == "test"


def test_tools_default_empty():
    """tools defaults to empty list."""
    entry = SessionLogEntry(id="x", when="t", user="u", metadata=EntryMetadata(model="m"))
    assert entry.tools == []
