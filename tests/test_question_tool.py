"""The root-only ``question`` tool: broker, commands, harness wiring and WebSocket flow."""

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import msgspec
import pytest
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.question import Answer, QuestionBroker, QuestionError, create_question_tool
from archie_shared.commands import AnswerCommand, decode_command, encode_command
from archie_shared.events import PendingQuestion, QuestionOption, SessionStatus, decode_event
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


def _question_llm(tool_input=QUESTION_INPUT, seen: list | None = None, configs=None):
    """A mock LLM that asks one question, then ends the turn with text."""
    calls = [0]

    def stream(messages, system, tool_config=None):
        if configs is not None:
            configs.append(tool_config)
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


def test_root_llm_is_offered_the_question_tool(mock_env):
    configs: list = []
    llm = _question_llm(configs=configs)
    with (
        patch.dict(os.environ, mock_env),
        patch("archie_agent.app.create_llm_client", return_value=llm),
    ):
        from archie_agent.app import app

        with TestClient(app) as client:
            _ask_and_answer(client, AnswerCommand(question_id="tu_q", dismiss=True))
    assert "question" in [tool["name"] for tool in configs[0]]


# --- M2: text, dismissal, validation, failure paths and /status -----------------------


def _ask_and_answer(client, command):
    """Run one question turn, answer it with ``command``, return the tool_result event."""
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(json.dumps({"type": "message", "content": "go"}))
        _receive_until(ws, _blocked)
        ws.send_text(encode_command(command))
        events = _receive_until(ws, lambda e: e["type"] == "turn_complete")
        return next(e for e in events if e["type"] == "tool_result")


def test_text_answer(question_client):
    result = _ask_and_answer(question_client(), AnswerCommand(question_id="tu_q", text="MySQL"))
    assert result["content"] == "User answered: MySQL"
    assert result["is_error"] is False


def test_dismiss_answer(question_client):
    result = _ask_and_answer(question_client(), AnswerCommand(question_id="tu_q", dismiss=True))
    assert result["content"] == (
        "User dismissed the question without answering. Do not assume an answer."
    )
    assert result["is_error"] is False


def test_turn_start_emits_working(question_client):
    client = question_client()
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(json.dumps({"type": "message", "content": "go"}))
        events = _receive_until(ws, lambda e: e["type"] == "session_status")
        assert [e["type"] for e in events][0] == "user_message"
        assert events[-1]["state"] == "working"
        _receive_until(ws, _blocked)
        ws.send_text(encode_command(AnswerCommand(question_id="tu_q", dismiss=True)))
        _receive_until(ws, lambda e: e["type"] == "turn_complete")


def test_reconnect_and_status_show_pending_question(question_client):
    client = question_client()
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(json.dumps({"type": "message", "content": "go"}))
        blocked = _receive_until(ws, _blocked)[-1]

        with client.websocket_connect("/stream") as second:
            second.receive_text()
            status = json.loads(second.receive_text())
            assert status["state"] == "blocked"
            assert status["question"] == blocked["question"]

        data = client.get("/status").json()
        assert data["state"] == "blocked"
        assert data["turn_active"] is True
        assert data["question"] == blocked["question"]

        ws.send_text(encode_command(AnswerCommand(question_id="tu_q", option=0)))
        _receive_until(ws, lambda e: e["type"] == "turn_complete")
        _receive_until(ws, lambda e: e["type"] == "session_status" and e["state"] == "idle")

    data = client.get("/status").json()
    assert data["state"] == "idle"
    assert data["question"] is None


def test_stale_or_out_of_range_answers_are_rejected(question_client):
    client = question_client()
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(json.dumps({"type": "message", "content": "go"}))
        _receive_until(ws, _blocked)

        ws.send_text(encode_command(AnswerCommand(question_id="other", option=0)))
        notice = _receive_until(ws, lambda e: e["type"] == "error_notice")[-1]
        assert notice["kind"] == "question_not_pending"

        ws.send_text(encode_command(AnswerCommand(question_id="tu_q", option=5)))
        notice = _receive_until(ws, lambda e: e["type"] == "error_notice")[-1]
        assert notice["kind"] == "invalid_answer"

        assert client.get("/status").json()["state"] == "blocked"
        ws.send_text(encode_command(AnswerCommand(question_id="tu_q", option=1)))
        events = _receive_until(ws, lambda e: e["type"] == "turn_complete")
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["content"] == "User selected option 2: Postgres"


def test_answer_without_pending_question_is_rejected(question_client):
    client = question_client()
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(encode_command(AnswerCommand(question_id="tu_q", dismiss=True)))
        notice = json.loads(ws.receive_text())
        assert notice["type"] == "error_notice"
        assert notice["kind"] == "question_not_pending"


def test_interrupt_while_waiting_clears_question(question_client):
    client = question_client()
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(json.dumps({"type": "message", "content": "go"}))
        _receive_until(ws, _blocked)
        ws.send_text(json.dumps({"type": "interrupt"}))
        events = _receive_until(ws, lambda e: e["type"] == "turn_interrupted")
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["content"] == "cancelled"
        assert result["is_error"] is True
        final = _receive_until(
            ws, lambda e: e["type"] == "session_status" and e["state"] == "idle"
        )[-1]
        assert final.get("question") is None

    assert client.get("/status").json()["question"] is None


@pytest.mark.parametrize(
    ("tool_input", "message"),
    [
        ({"question": " ", "options": QUESTION_INPUT["options"]}, "question must be"),
        ({"question": "Q?", "options": [{"label": "only"}]}, "options must be a list of 2 to 4"),
        ({"question": "Q?", "options": [{"label": str(i)} for i in range(5)]}, "2 to 4"),
        ({"question": "Q?", "options": [{"label": "a"}, {"label": ""}]}, "options[1].label"),
        ({"question": "Q?", "options": [{"label": "a"}, "b"]}, "options[1] must be an object"),
        (
            {"question": "Q?", "options": [{"label": "a"}, {"label": "b"}], "recommended": 2},
            "recommended must be",
        ),
        (
            {"question": "Q?", "options": [{"label": "a"}, {"label": "b"}], "recommended": True},
            "recommended must be",
        ),
    ],
)
async def test_invalid_input_returns_error_without_blocking(tool_input, message):
    changes: list = []

    async def on_change():
        changes.append(broker.pending)

    broker = QuestionBroker(on_change=on_change)
    spec = create_question_tool(broker)
    result = await spec.handler(**tool_input, _tool_use_id="tu")
    assert result.startswith("Error: ")
    assert message in result
    assert changes == []
    assert broker.pending is None


def _question(question_id="q1"):
    return PendingQuestion(
        id=question_id,
        question="Q?",
        options=[QuestionOption(label="a"), QuestionOption(label="b")],
    )


async def test_broker_allows_one_pending_question():
    broker = QuestionBroker()
    first = asyncio.create_task(broker.ask(_question("q1")))
    await asyncio.sleep(0)
    assert broker.pending is not None and broker.pending.id == "q1"

    spec = create_question_tool(broker)
    second = await spec.handler(
        question="Other?", options=[{"label": "x"}, {"label": "y"}], _tool_use_id="q2"
    )
    assert second == "Error: another question is already waiting"
    assert broker.pending.id == "q1"

    broker.answer("q1", Answer(text="done"))
    assert (await first).text == "done"
    assert broker.pending is None


async def test_broker_cancellation_clears_pending_and_notifies():
    states: list = []

    async def on_change():
        states.append(broker.pending)

    broker = QuestionBroker(on_change=on_change)
    task = asyncio.create_task(broker.ask(_question()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending is None
    assert [s.id if s else None for s in states] == ["q1", None]
    with pytest.raises(QuestionError) as exc:
        broker.answer("q1", Answer(dismiss=True))
    assert exc.value.kind == "question_not_pending"


async def test_broker_status_failure_does_not_block_answer():
    async def on_change():
        raise RuntimeError("bus down")

    broker = QuestionBroker(on_change=on_change)
    task = asyncio.create_task(broker.ask(_question()))
    await asyncio.sleep(0)
    broker.answer("q1", Answer(option=0))
    assert (await task).option == 0


@pytest.mark.parametrize(
    "raw",
    [
        '{"type":"answer","question_id":"q"}',
        '{"type":"answer","question_id":"q","option":0,"text":"x"}',
        '{"type":"answer","question_id":"q","option":0,"dismiss":true}',
        '{"type":"answer","question_id":"q","text":"  "}',
        '{"type":"answer","question_id":"q","option":true}',
        '{"type":"answer","question_id":"q","dismiss":true,"extra":1}',
    ],
)
def test_answer_command_rejects_malformed(raw):
    with pytest.raises((ValueError, TypeError, msgspec.ValidationError)):
        decode_command(raw)


@pytest.mark.parametrize(
    "command",
    [
        AnswerCommand(question_id="q", option=0),
        AnswerCommand(question_id="q", text="hello"),
        AnswerCommand(question_id="q", dismiss=True),
    ],
)
def test_answer_command_round_trip(command):
    raw = encode_command(command)
    assert json.loads(raw)["type"] == "answer"
    assert decode_command(raw) == command


def test_session_status_defaults_and_round_trip():
    status = SessionStatus(id="s", model_key="m", git_branch="main")
    assert status.state == "idle"
    assert status.question is None
    blocked = SessionStatus(
        id="s2", model_key="m", git_branch="main", state="blocked", question=_question()
    )
    decoded = decode_event(msgspec.json.encode(blocked))
    assert decoded == blocked
    assert not decoded.persist


async def test_kiro_status_and_answer_have_no_question():
    from archie_agent.kiro.harness import KiroHarness

    harness = KiroHarness.__new__(KiroHarness)
    harness._turn_active = False
    harness.session = MagicMock(model_id="kiro/claude")
    with patch("archie_agent.app._read_git_branch", return_value="main"):
        status = harness.status_event()
    assert (status.state, status.question) == ("idle", None)
    harness._turn_active = True
    with patch("archie_agent.app._read_git_branch", return_value="main"):
        assert harness.status_event().state == "working"
    notice = harness.answer_question(AnswerCommand(question_id="q", dismiss=True))
    assert notice.kind == "question_not_pending"
    assert harness.pending_question is None


def test_manual_compaction_emits_working(question_client):
    client = question_client()
    with client.websocket_connect("/stream") as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(json.dumps({"type": "message", "content": "/compact"}))
        first = _receive_until(ws, lambda e: e["type"] == "session_status")[-1]
        assert first["state"] == "working"
        last = _receive_until(ws, lambda e: e["type"] == "session_status")[-1]
        assert last["state"] == "idle"


@pytest.mark.parametrize(
    "answer",
    [Answer(option=1), Answer(text="MySQL"), Answer(dismiss=True)],
)
def test_result_text_round_trips_through_row_summary(answer):
    from archie_agent.question import format_answer
    from archie_shared.tool_summaries import format_tool_complete

    question = _question()
    summary = format_tool_complete(
        "question", {"question": "Q?"}, format_answer(question, answer), False
    )
    assert "chars)" not in summary
    assert ("→" in summary) or ("(dismissed)" in summary)
