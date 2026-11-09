"""The root-only ``question`` tool: broker, commands, harness wiring and WebSocket flow."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_shared.commands import AnswerCommand, encode_command
from starlette.testclient import TestClient

QUESTION_INPUT = {
    "question": "Which database?",
    "options": [
        {"label": "SQLite", "description": "Zero setup"},
        {"label": "Postgres"},
    ],
    "recommended": 0,
}


@pytest.fixture
def mock_env(tmp_path):
    home_dir = tmp_path / "nexus_home"
    home_dir.mkdir()
    (home_dir / "config.yaml").write_text(
        'global:\n  model: "bedrock-claude-sonnet-4-6"\n  region: "eu-west-1"\n'
    )
    return {"ARCHIE_HOME_DIR": str(home_dir), "ARCHIE_SESSION_ID": "test-session"}


def _question_llm(tool_input=QUESTION_INPUT, seen: list | None = None):
    """A mock LLM that asks one question, then ends the turn with text."""
    calls = [0]

    def stream(messages, system, tool_config=None):
        if calls[0] == 0:
            calls[0] += 1
            yield ToolUseStart(tool_use_id="tu_q", name="question")
            yield ToolUseEvent(tool_use_id="tu_q", name="question", input=tool_input)
            yield Usage(input_tokens=10, output_tokens=5)
            yield Done(stop_reason="tool_use")
        else:
            if seen is not None:
                seen.append(messages)
            yield TextDelta(text="ok")
            yield Usage(input_tokens=20, output_tokens=2)
            yield Done(stop_reason="end_turn")

    mock = MagicMock()
    mock.model_id = "eu.anthropic.claude-sonnet-4-6"
    mock.stream = stream
    return mock


def _receive_until(ws, predicate):
    events = []
    while True:
        event = json.loads(ws.receive_text())
        events.append(event)
        if predicate(event):
            return events


def _blocked(event):
    return event["type"] == "session_status" and event.get("state") == "blocked"


def _connect(client):
    ws = client.websocket_connect("/stream")
    conn = ws.__enter__()
    conn.receive_text()  # handshake
    status = json.loads(conn.receive_text())
    return ws, conn, status


@pytest.fixture
def question_client(mock_env):
    def make(tool_input=QUESTION_INPUT, seen=None):
        llm = _question_llm(tool_input, seen)
        env = patch.dict(os.environ, mock_env)
        create = patch("archie_agent.app.create_llm_client", return_value=llm)
        env.start()
        create.start()
        from archie_agent.app import app

        client = TestClient(app)
        client.__enter__()
        cleanups.append(lambda: (client.__exit__(None, None, None), create.stop(), env.stop()))
        return client

    cleanups: list = []
    yield make
    for cleanup in reversed(cleanups):
        cleanup()


# --- M1: option answer end to end ------------------------------------------------------


def test_option_answer_round_trip(question_client):
    seen: list = []
    client = question_client(seen=seen)
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        initial = json.loads(ws.receive_text())
        assert initial["type"] == "session_status"
        assert initial["state"] == "idle"
        assert initial.get("question") is None

        ws.send_text(json.dumps({"type": "message", "content": "pick a db"}))
        events = _receive_until(ws, _blocked)
        blocked = events[-1]
        assert any(e["type"] == "tool_call" and e["name"] == "question" for e in events)
        assert blocked["question"] == {
            "id": "tu_q",
            "question": "Which database?",
            "options": [
                {"label": "SQLite", "description": "Zero setup"},
                {"label": "Postgres", "description": ""},
            ],
            "recommended": 0,
        }

        ws.send_text(encode_command(AnswerCommand(question_id="tu_q", option=1)))
        events = _receive_until(ws, lambda e: e["type"] == "turn_complete")
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["content"] == "User selected option 2: Postgres"
        assert result["is_error"] is False
        unblocked = [e for e in events if e["type"] == "session_status"]
        assert unblocked[0]["state"] == "working"
        assert unblocked[0].get("question") is None

        final = json.loads(ws.receive_text())
        assert final["type"] == "session_status"
        assert final["state"] == "idle"

    tool_results = [
        block
        for turn in seen[0]
        if isinstance(turn.content, list)
        for block in turn.content
        if getattr(block, "tool_use_id", None) == "tu_q" and hasattr(block, "is_error")
    ]
    assert tool_results[0].content == "User selected option 2: Postgres"


def test_question_is_root_only(mock_env):
    from archie_agent.harness import AgentHarness

    with patch.dict(os.environ, mock_env):
        from archie_agent.app import app

        llm = _question_llm()
        with patch("archie_agent.app.create_llm_client", return_value=llm):
            with TestClient(app):
                from archie_agent import app as app_module

                harness = app_module._agent
                assert isinstance(harness, AgentHarness)
                names = [tool["name"] for tool in harness._tool_config]
                assert "question" in names
