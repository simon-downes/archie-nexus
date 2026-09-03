import sqlite3

import pytest
from archie_orchestrator.metrics import MetricsWriter
from archie_shared.events import (
    LLMRequest,
    SessionStarted,
    ToolResult,
    decode_event,
    encode_event,
)
from archie_shared.session.log import EventIdConflict, SessionLog


def request(event_id="01J00000000000000000000000", sent_at="2025-01-01T00:00:00+00:00"):
    return LLMRequest(
        id=event_id,
        scope=None,
        turn=2,
        iteration=1,
        model_key="m",
        sent_at=sent_at,
        duration_ms=12,
        status="completed",
        input_tokens=10,
        output_tokens=4,
        cache_read_tokens=2,
        cache_write_tokens=1,
        context_tokens=13,
        cost_usd=0.123456,
    )


def test_canonical_round_trip_and_order(tmp_path):
    path = tmp_path / "session.jsonl"
    session_log = SessionLog(path)
    session_log.append(
        SessionStarted(
            id="01J00000000000000000000001", schema_version=2, sent_at="now", model_key="m"
        )
    )
    event = request()
    session_log.append(event)
    line = encode_event(event)
    assert path.read_text().splitlines()[-1] == line
    assert session_log.read()[1] == event
    assert decode_event(line, persisted=True) == event


def test_old_tool_result_without_metadata_decodes_with_defaults():
    raw = (
        '{"type":"tool_result","id":"01J00000000000000000000002",'
        '"turn":1,"iteration":1,"scope":null,"request_id":"req",'
        '"tool_use_id":"tool","content":"old result","is_error":false}'
    )
    event = decode_event(raw, persisted=True)
    assert isinstance(event, ToolResult)
    assert event.duration_ms == 0
    assert event.result_bytes == 0
    assert event.result_lines == 0


def test_duplicate_events_are_idempotent_and_conflicts_rejected(tmp_path):
    path = tmp_path / "session.jsonl"
    event = request()
    session_log = SessionLog(path)
    session_log.append(event)
    assert session_log.append(event) is False
    assert len(path.read_text().splitlines()) == 1
    conflicting = request(sent_at="later")
    with pytest.raises(EventIdConflict):
        session_log.append(conflicting)


def test_canonical_metrics_are_idempotent(tmp_path):
    path = tmp_path / "metrics.db"
    writer = MetricsWriter(path)
    conn = sqlite3.connect(path)
    writer._ensure_schema(conn)
    raw = encode_event(request())
    writer._process_batch(conn, [("s", raw), ("s", raw)])
    assert conn.execute("select count(*) from requests").fetchone()[0] == 1
    conn.close()


def test_llm_request_round_trip():
    """Canonical llm_request frames decode through the single event path."""
    event = request()
    raw = encode_event(event)
    restored = decode_event(raw)
    assert isinstance(restored, LLMRequest)
    assert restored == event
    assert restored.cost_usd == event.cost_usd
    assert restored.input_tokens == event.input_tokens
    assert restored.output_tokens == event.output_tokens
