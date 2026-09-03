"""Tests for session log persistence (log.py) — per-message schema."""

import msgspec
import pytest
from archie_shared.events import SessionStarted, TextDelta, UserMessage, decode_event, encode_event
from archie_shared.session.log import CursorNotFound, EventIdConflict, LogAppendError, SessionLog
from archie_shared.session.migrate import MessageEntry, MessageMetadata


def _write_legacy(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(msgspec.json.encode(entry).decode() + "\n")


def test_write_user_entry(tmp_path):
    """User entries have metadata=None and create file + parent dirs."""
    path = tmp_path / "sessions" / "test.jsonl"
    entry = MessageEntry(
        id="01J3ABCDEF",
        when="2026-07-07T12:00:00+00:00",
        role="user",
        content="hello",
    )
    _write_legacy(path, entry)
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
    _write_legacy(path, entry)

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
    _write_legacy(path, entry)

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
        _write_legacy(path, entry)

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
    _write_legacy(path, entry)

    line = path.read_text().strip()
    decoded = msgspec.json.decode(line, type=MessageEntry)
    assert decoded.content == ""
    assert decoded.metadata is not None
    assert decoded.metadata.interrupted is True


def _session_started(event_id: str = "01J00000000000000000000001") -> SessionStarted:
    return SessionStarted(id=event_id, schema_version=2, sent_at="now", model_key="m")


def test_session_log_scans_canonical_events_and_skips_invalid_lines(tmp_path, caplog):
    path = tmp_path / "session.jsonl"
    event = _session_started()
    canonical = encode_event(event)
    path.write_text(
        "not json\n"
        + encode_event(
            TextDelta(
                id="01J00000000000000000000002",
                turn=1,
                iteration=0,
                scope=None,
                request_id="r",
                text="live",
            )
        )
        + "\n"
        + canonical.replace(',"model_key"', ', "model_key"')
        + "\n"
    )

    session_log = SessionLog(path)

    assert session_log.read() == [event]
    assert session_log.append(event) is False
    assert "line 1" in caplog.text
    assert "line 2" in caplog.text


def test_session_log_rejects_conflicting_duplicate_ids(tmp_path):
    path = tmp_path / "session.jsonl"
    event = _session_started()
    conflict = _session_started()
    conflict = SessionStarted(id=event.id, schema_version=2, sent_at="later", model_key="m")
    path.write_text(encode_event(event) + "\n" + encode_event(conflict) + "\n")

    with pytest.raises(EventIdConflict):
        SessionLog(path)


def test_session_log_append_is_idempotent_and_cursor_keyed(tmp_path):
    session_log = SessionLog(tmp_path / "session.jsonl")
    first = _session_started()
    second = UserMessage(id="01J00000000000000000000002", turn=1, scope=None, content="hello")

    assert session_log.append(first) is True
    assert session_log.append(first) is False
    assert session_log.append(second) is True
    assert session_log.read(after_id=first.id) == [second]
    with pytest.raises(CursorNotFound):
        session_log.read(after_id="01J00000000000000000000099")


def test_session_log_rejects_live_events_and_distinguishes_append_failures(tmp_path, monkeypatch):
    session_log = SessionLog(tmp_path / "session.jsonl")
    live = TextDelta(
        id="01J00000000000000000000001", turn=1, iteration=0, scope=None, request_id="r", text="x"
    )
    with pytest.raises(TypeError):
        session_log.append(live)
    with pytest.raises(ValueError, match="live-only"):
        decode_event(encode_event(live), persisted=True)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("archie_shared.session.log._append_serialized_event", fail)
    with pytest.raises(LogAppendError, match="disk full"):
        session_log.append(_session_started())
