"""Integration tests for WebSocket endpoint with mocked Bedrock."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from archie_agent.llm._types import Done, TextDelta, Usage
from starlette.testclient import TestClient


def _make_mock_bedrock_client(events: list):
    """Create a mock BedrockClient that yields events from stream()."""
    mock = MagicMock()
    mock.model_id = "eu.anthropic.claude-sonnet-4-6"

    def stream(messages, system, tool_config=None):
        yield from events

    mock.stream = stream
    return mock


@pytest.fixture
def mock_env(tmp_path):
    """Set up environment with a config file in the new format."""
    home_dir = tmp_path / "nexus_home"
    home_dir.mkdir()
    config_file = home_dir / "config.yaml"
    config_file.write_text('global:\n  model: "bedrock-claude-sonnet-4-6"\n  region: "eu-west-1"\n')
    return {"ARCHIE_HOME_DIR": str(home_dir), "ARCHIE_SESSION_ID": "test-session"}


@pytest.fixture
def mock_events():
    return [
        TextDelta(text="Hello"),
        TextDelta(text=" there"),
        Usage(input_tokens=100, output_tokens=10, cache_read_input_tokens=50),
        Done(stop_reason="end_turn"),
    ]


@pytest.fixture
def client(mock_env, mock_events):
    """Create a TestClient with mocked LLM client."""
    with patch.dict(os.environ, mock_env):
        mock_bedrock = _make_mock_bedrock_client(mock_events)
        with patch("archie_agent.app.create_llm_client", return_value=mock_bedrock):
            from archie_agent.app import app

            with TestClient(app) as c:
                yield c


def test_status_includes_session_metadata(client):
    """Verify /status returns model and session info."""
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model"] == "bedrock-claude-sonnet-4-6"
    assert data["session_id"] == "test-session"
    assert data["turn_count"] == 0


def test_events_on_start_has_only_session_started(client):
    """GET /events on a fresh session returns just the session_started event."""
    resp = client.get("/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in resp.text.splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["type"] == "session_started"


def _run_turn(client):
    """Drive one message turn to completion so the canonical log is populated."""
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()  # session_snapshot
        ws.receive_text()  # session_info
        ws.send_text(json.dumps({"type": "message", "data": {"content": "hello"}}))
        while True:
            if json.loads(ws.receive_text())["type"] == "turn_complete":
                break


def _events_lines(client, after=None):
    path = "/events" + (f"?after={after}" if after else "")
    resp = client.get(path)
    assert resp.status_code == 200
    return [json.loads(line) for line in resp.text.splitlines() if line]


def test_events_complete_log_after_turn(client):
    """GET /events replays the full canonical log as ordered NDJSON."""
    _run_turn(client)
    events = _events_lines(client)
    assert len(events) > 0
    types = [e["type"] for e in events]
    assert "user_message" in types
    assert "assistant_message" in types
    # text_delta is live-only and must NOT be persisted/replayed
    assert "text_delta" not in types
    # every persisted event carries an id
    assert all(e.get("id") for e in events)


def test_events_valid_cursor_slices_after(client):
    """GET /events?after=<id> returns only events strictly after the cursor."""
    _run_turn(client)
    events = _events_lines(client)
    assert len(events) >= 2
    cursor = events[0]["id"]
    tail = _events_lines(client, after=cursor)
    assert [e["id"] for e in tail] == [e["id"] for e in events[1:]]


def test_events_unknown_cursor_returns_409(client):
    """GET /events?after=<unknown> returns 409 so the TUI falls back to full replay."""
    _run_turn(client)
    resp = client.get("/events?after=01J000000000000000000000ZZ")
    assert resp.status_code == 409
    assert resp.json()["error"] == "cursor_not_found"


def test_websocket_session_info_on_connect(client):
    """Verify WS connect sends SessionSnapshot then SessionInfo events."""
    with client.websocket_connect("/stream") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "session_snapshot"
        assert snapshot["data"]["protocol_version"] == 1
        assert snapshot["data"]["model"] == "Claude Sonnet 4.6"
        assert snapshot["data"]["session_id"] == "test-session"
        assert isinstance(snapshot["data"]["latest_event_id"], str)
        assert snapshot["data"]["accounting"]["total_cost"] == 0.0

        data = json.loads(ws.receive_text())
        assert data["type"] == "session_info"
        assert data["data"]["protocol_version"] == 1
        assert data["data"]["model"] == "Claude Sonnet 4.6"
        assert data["data"]["session_id"] == "test-session"


def test_websocket_message_and_events(client):
    """Verify sending a message yields text_delta and turn_complete events."""
    with client.websocket_connect("/stream") as ws:
        # Consume session_snapshot + session_info
        session_snapshot = json.loads(ws.receive_text())
        assert session_snapshot["type"] == "session_snapshot"
        session_info = json.loads(ws.receive_text())
        assert session_info["type"] == "session_info"

        # Send a message
        ws.send_text(json.dumps({"type": "message", "data": {"content": "hello"}}))

        # Collect events until turn_complete
        events = []
        while True:
            raw = ws.receive_text()
            event = json.loads(raw)
            events.append(event)
            if event["type"] == "turn_complete":
                break

        # Should have text_delta(s), usage, turn_complete
        types = [e["type"] for e in events]
        assert "text_delta" in types
        assert "usage" in types
        assert types[-1] == "turn_complete"

        # Wire events carry turn_index. The `llm_request` frame is a raw canonical
        # event broadcast for the metrics pipeline and has no wire turn_index.
        for e in events:
            if e["type"] == "llm_request":
                continue
            assert e["turn_index"] == 1


# --- Tool turn integration test ---


def test_websocket_tool_turn(mock_env, tmp_path):
    """Verify a tool turn sends tool_call and tool_result events over WS."""
    from archie_agent.llm._types import ToolUseEvent, ToolUseStart

    # First response: tool_use; second response: text answer
    tool_events = [
        ToolUseStart(tool_use_id="tu_1", name="exec"),
        ToolUseEvent(
            tool_use_id="tu_1",
            name="exec",
            input={"source": "async def main(): return 42\n"},
        ),
        Usage(input_tokens=100, output_tokens=20),
        Done(stop_reason="tool_use"),
    ]
    text_events = [
        TextDelta(text="The answer is 42"),
        Usage(input_tokens=200, output_tokens=15),
        Done(stop_reason="end_turn"),
    ]

    call_count = [0]

    def stream_fn(messages, system, tool_config=None):
        if call_count[0] == 0:
            call_count[0] += 1
            yield from tool_events
        else:
            yield from text_events

    mock_bedrock = MagicMock()
    mock_bedrock.model_id = "eu.anthropic.claude-sonnet-4-6"
    mock_bedrock.stream = stream_fn

    with patch.dict(os.environ, mock_env):
        with patch("archie_agent.app.create_llm_client", return_value=mock_bedrock):
            # Simpler approach: just patch run_exec to avoid needing the subprocess
            from unittest.mock import AsyncMock

            from archie_agent.app import app
            from archie_agent.exec.envelope import Envelope

            fake_envelope = Envelope(ok=True, return_value=42, duration_ms=10)

            with patch("archie_agent.harness.run_exec", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = fake_envelope

                with TestClient(app) as client:
                    with client.websocket_connect("/stream") as ws:
                        # Consume session_snapshot + session_info
                        ws.receive_text()
                        ws.receive_text()

                        # Send message
                        ws.send_text(
                            json.dumps({"type": "message", "data": {"content": "what is 21*2"}})
                        )

                        # Collect events until turn_complete
                        events = []
                        while True:
                            raw = ws.receive_text()
                            event = json.loads(raw)
                            events.append(event)
                            if event["type"] in ("turn_complete", "turn_error"):
                                break

                        types = [e["type"] for e in events]
                        assert "tool_call" in types
                        assert "tool_result" in types
                        assert "text_delta" in types
                        assert types[-1] == "turn_complete"

                        # Verify tool_call event content
                        tc = next(e for e in events if e["type"] == "tool_call")
                        assert tc["data"]["name"] == "exec"

                        # Verify tool_result event
                        tr = next(e for e in events if e["type"] == "tool_result")
                        assert tr["data"]["is_error"] is False
