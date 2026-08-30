import sqlite3

from archie_orchestrator.metrics import MetricsWriter
from archie_shared.canonical_events import (
    LLMRequest,
    SessionStarted,
    ToolResult,
    decode_event,
    encode_event,
)
from archie_shared.session.log import append_event, read_events


def request(event_id="01J00000000000000000000000"):
    return LLMRequest(
        id=event_id,
        scope=None,
        turn_iteration="2.1",
        model_key="m",
        sent_at="2025-01-01T00:00:00+00:00",
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
    append_event(
        path,
        SessionStarted(
            id="01J00000000000000000000001", schema_version=1, sent_at="now", model_key="m"
        ),
    )
    event = request()
    line = append_event(path, event)
    assert path.read_text().splitlines()[-1] == line
    assert read_events(path)[1] == event
    assert decode_event(line, persisted=True) == event


def test_old_tool_result_without_metadata_decodes_with_defaults():
    raw = (
        '{"type":"tool_result","id":"01J00000000000000000000002",'
        '"turn_iteration":"1.1","scope":null,"request_id":"req",'
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
    append_event(path, event)
    append_event(path, event, encode_event(event))
    assert len(path.read_text().splitlines()) == 1
    try:
        append_event(path, event, encode_event(request()) + " ")
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting duplicate was accepted")


def test_canonical_metrics_are_idempotent(tmp_path):
    path = tmp_path / "metrics.db"
    writer = MetricsWriter(path)
    conn = sqlite3.connect(path)
    writer._ensure_schema(conn)
    raw = encode_event(request())
    writer._process_batch(conn, [("s", raw), ("s", raw)])
    assert conn.execute("select count(*) from requests").fetchone()[0] == 1
    conn.close()


def test_llm_request_wire_deserialize_round_trip():
    """The broadcast llm_request frame (raw canonical JSON, no wire `data`
    envelope) must decode through the wire deserializer with cost/tokens
    intact so the TUI can consume it for authoritative live accounting."""
    from archie_shared.events import deserialize_event

    event = request()
    raw = encode_event(event)  # raw canonical JSON, top-level `type`/`cost_usd`
    assert '"data"' not in raw

    restored = deserialize_event(raw)
    assert isinstance(restored, LLMRequest)
    assert restored == event
    assert restored.cost_usd == event.cost_usd
    assert restored.input_tokens == event.input_tokens
    assert restored.output_tokens == event.output_tokens
