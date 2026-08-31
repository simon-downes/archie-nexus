"""Tests for the AgentHarness — full path from message to wire events + persistence."""

import json

import pytest
from archie_agent.harness import AgentHarness, _shell_result_is_error
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_shared.canonical_events import (
    AssistantMessage,
    LLMRequest,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UserMessage,
    decode_event,
)
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry


def test_shell_result_error_uses_only_first_line():
    assert _shell_result_is_error("[exit: 0]\noutput mentions [exit: 123] and error: text") is False
    assert _shell_result_is_error("[exit: 123]\noutput") is True
    assert _shell_result_is_error("[error: command timed out]\noutput") is True
    assert _shell_result_is_error("output mentions [exit: 123]") is False


def _read_events(log_path) -> list:
    """Decode all persisted canonical events from a session JSONL log."""
    return [
        decode_event(line, persisted=True)
        for line in log_path.read_bytes().splitlines()
        if line.strip()
    ]


def _of(events, cls) -> list:
    return [e for e in events if isinstance(e, cls)]


# --- Fixtures ---


def _make_model() -> ModelEntry:
    """Create a test ModelEntry."""
    return ModelEntry(
        name="Test Model",
        provider=BedrockProvider(model_id="test-model-id", region="us-east-1"),
        cost=CostConfig(input=3.0, output=15.0),
        context=200000,
        max_output_tokens=4096,
    )


def _make_harness(
    tmp_path,
    responses: list[list],
    delay: float = 0.0,
    *,
    exec_python: str | None = None,
    exec_run_root=None,
) -> AgentHarness:
    """Create a harness with FakeLLMClient and a tmp log dir."""
    model = _make_model()
    session = Session(
        model_id="test-model",
        model=model,
        session_id="test-session-001",
    )
    llm = FakeLLMClient(responses=responses, delay=delay)
    return AgentHarness(
        session=session,
        llm_client=llm,
        model_name="Test Model",
        log_dir=tmp_path,
        exec_python=exec_python,
        exec_run_root=exec_run_root,
    )


class FakeWebSocket:
    """Captures sent messages for assertion."""

    def __init__(self):
        self.messages: list[str] = []
        self.closed = False

    async def send_text(self, data: str) -> None:
        self.messages.append(data)

    def parsed_events(self) -> list[dict]:
        return [json.loads(m) for m in self.messages]


# --- Tests ---


@pytest.mark.asyncio
async def test_normal_flow(tmp_path):
    """Complete message flow: text + usage + done → wire events + persistence."""
    harness = _make_harness(
        tmp_path,
        responses=[
            [
                TextDelta(text="Hello "),
                TextDelta(text="world"),
                Usage(
                    input_tokens=100,
                    output_tokens=10,
                    cache_read_input_tokens=5,
                    cache_write_input_tokens=2,
                ),
                Done(stop_reason="end_turn"),
            ]
        ],
    )

    ws = FakeWebSocket()
    harness.clients.add(ws)

    await harness.handle_message("Hi there")

    events = ws.parsed_events()
    event_types = [e["type"] for e in events]

    # Verify wire event sequence
    assert "text_delta" in event_types
    # Usage is internal; the public ledger is llm_request.
    assert "usage" not in event_types
    assert "turn_complete" in event_types

    # Verify session state
    assert harness.session.total_input_tokens == 100
    assert harness.session.total_output_tokens == 10
    assert len(harness.session.turns) == 2  # user + assistant

    # Verify canonical event persistence
    log_path = tmp_path / "test-session-001.jsonl"
    assert log_path.exists()
    events = _read_events(log_path)

    # text_delta is live-only (not persisted); expect the canonical sequence.
    user_msgs = _of(events, UserMessage)
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "Hi there"
    assert user_msgs[0].turn == 1

    requests = _of(events, LLMRequest)
    assert len(requests) == 1
    assert requests[0].status == "completed"
    assert requests[0].input_tokens == 100
    assert requests[0].output_tokens == 10
    assert requests[0].cost_usd > 0

    assistant_msgs = _of(events, AssistantMessage)
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].content == "Hello world"
    assert assistant_msgs[0].interrupted is False
    assert assistant_msgs[0].request_ids == [requests[0].id]

    assert len(_of(events, TurnComplete)) == 1


@pytest.mark.asyncio
async def test_interrupt_mid_stream(tmp_path):
    """Interrupt during streaming → TurnInterrupted wire event + partial persistence."""
    harness = _make_harness(
        tmp_path,
        responses=[
            [
                TextDelta(text="partial"),
                TextDelta(text=" text"),
                TextDelta(text=" more"),
                Done(stop_reason="end_turn"),
            ]
        ],
        delay=0.05,
    )

    ws = FakeWebSocket()
    harness.clients.add(ws)

    # Set interrupt shortly after start
    async def _interrupt_soon():
        import asyncio

        await asyncio.sleep(0.08)
        harness.interrupt()

    import asyncio

    task = asyncio.create_task(_interrupt_soon())
    await harness.handle_message("Tell me a story")
    await task

    events = ws.parsed_events()
    event_types = [e["type"] for e in events]

    assert "turn_interrupted" in event_types
    assert "turn_complete" not in event_types

    # Verify persistence: user + partial assistant (interrupted) + turn_interrupted
    log_path = tmp_path / "test-session-001.jsonl"
    events = _read_events(log_path)

    assert len(_of(events, UserMessage)) == 1

    assistant_msgs = _of(events, AssistantMessage)
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].interrupted is True

    assert len(_of(events, TurnInterrupted)) == 1
    assert len(_of(events, TurnComplete)) == 0


@pytest.mark.asyncio
async def test_llm_error(tmp_path):
    """LLM exception → TurnError wire event."""

    class _ErrorLLM:
        model_id = "test-error"

        def stream(self, messages, system, tool_config=None):
            raise RuntimeError("Service unavailable")
            yield  # noqa: RET503

        def invoke(self, messages, system):
            return ""

    model = _make_model()
    session = Session(model_id="test", model=model, session_id="err-session")
    harness = AgentHarness(
        session=session,
        llm_client=_ErrorLLM(),
        model_name="Test Model",
        log_dir=tmp_path,
    )

    ws = FakeWebSocket()
    harness.clients.add(ws)

    await harness.handle_message("hello")

    events = ws.parsed_events()
    event_types = [e["type"] for e in events]
    assert "turn_error" in event_types

    # Turn should be released
    assert harness.turn_active is False


@pytest.mark.asyncio
async def test_turn_already_active(tmp_path):
    """Second message while turn active → TurnError without invoking loop."""
    harness = _make_harness(
        tmp_path,
        responses=[
            [
                TextDelta(text="slow"),
                Done(stop_reason="end_turn"),
            ]
        ],
        delay=0.1,
    )

    ws = FakeWebSocket()
    harness.clients.add(ws)

    import asyncio

    # Start first message (will be slow due to delay)
    task = asyncio.create_task(harness.handle_message("first"))
    await asyncio.sleep(0.02)  # Let it start

    # Try second message while first is active
    assert harness.turn_active is True
    await harness.handle_message("second")

    # Should get a live-only error_notice for the rejected message.
    events = ws.parsed_events()
    error_events = [e for e in events if e["type"] == "error_notice"]
    assert len(error_events) >= 1
    assert "already active" in error_events[0]["message"]

    await task


@pytest.mark.asyncio
async def test_usage_updates_context_pct(tmp_path):
    """Usage updates session._last_input_tokens for context_pct."""
    harness = _make_harness(
        tmp_path,
        responses=[
            [
                TextDelta(text="x"),
                Usage(input_tokens=50000, output_tokens=100),
                Done(stop_reason="end_turn"),
            ]
        ],
    )

    await harness.handle_message("test")

    # context_pct should be based on the last request's context input.
    # 50000 / 200000 context * 100 = 25%
    assert harness.session.context_pct == pytest.approx(25.0)
    assert harness.session._last_input_tokens == 50000


@pytest.mark.asyncio
async def test_per_message_cost_not_cumulative(tmp_path):
    """Each assistant entry has per-message cost, not cumulative."""
    harness = _make_harness(
        tmp_path,
        responses=[
            [
                TextDelta(text="first"),
                Usage(input_tokens=100, output_tokens=10),
                Done(stop_reason="end_turn"),
            ],
            [
                TextDelta(text="second"),
                Usage(input_tokens=200, output_tokens=20),
                Done(stop_reason="end_turn"),
            ],
        ],
    )

    await harness.handle_message("msg1")
    await harness.handle_message("msg2")

    log_path = tmp_path / "test-session-001.jsonl"
    events = _read_events(log_path)

    requests = _of(events, LLMRequest)
    assert len(requests) == 2
    r1, r2 = requests

    # Each request carries its own per-message tokens/cost, not cumulative.
    assert r1.input_tokens == 100
    assert r2.input_tokens == 200
    # Second cost should be larger (more tokens)
    assert r2.cost_usd > r1.cost_usd
    # Neither should equal session total_cost
    assert r1.cost_usd != harness.session.total_cost


@pytest.mark.asyncio
async def test_partial_text_before_error(tmp_path):
    """LLM yields text then errors → partial assistant persisted + TurnError."""

    class _PartialErrorLLM:
        model_id = "test-partial-error"

        def stream(self, messages, system, tool_config=None):
            yield TextDelta(text="partial ")
            yield TextDelta(text="response")
            raise RuntimeError("Connection reset")

        def invoke(self, messages, system):
            return ""

    model = _make_model()
    session = Session(model_id="test", model=model, session_id="partial-err")
    harness = AgentHarness(
        session=session,
        llm_client=_PartialErrorLLM(),
        model_name="Test Model",
        log_dir=tmp_path,
    )

    ws = FakeWebSocket()
    harness.clients.add(ws)

    await harness.handle_message("hello")

    events = ws.parsed_events()
    event_types = [e["type"] for e in events]

    # Should have text deltas AND a turn_error
    assert "text_delta" in event_types
    assert "turn_error" in event_types

    # Partial text should be persisted as an interrupted assistant message
    log_path = tmp_path / "partial-err.jsonl"
    events = _read_events(log_path)

    assistant_msgs = _of(events, AssistantMessage)
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].content == "partial response"
    assert assistant_msgs[0].interrupted is True

    turn_errors = _of(events, TurnError)
    assert len(turn_errors) == 1
    assert "Connection reset" in turn_errors[0].message

    # Turn should be released
    assert harness.turn_active is False


# --- Tool orchestration tests ---


@pytest.mark.asyncio
async def test_tool_round_trip_persists_and_broadcasts(tmp_path):
    """Full exec round-trip: persists ordered entries + broadcasts tool events."""
    import sys

    from archie_agent.llm._types import ToolUseEvent, ToolUseStart

    harness = _make_harness(
        tmp_path,
        responses=[
            # First: model calls exec
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(
                    tool_use_id="tu_1",
                    name="exec",
                    input={"source": 'async def main():\n    return "hello"\n'},
                ),
                Usage(input_tokens=100, output_tokens=20),
                Done(stop_reason="tool_use"),
            ],
            # Second: model answers
            [
                TextDelta(text="The result is hello"),
                Usage(input_tokens=200, output_tokens=15),
                Done(stop_reason="end_turn"),
            ],
        ],
        exec_python=sys.executable,
        exec_run_root=tmp_path / "runs",
    )

    ws = FakeWebSocket()
    harness.clients.add(ws)

    await harness.handle_message("run some code")

    # Check canonical event log
    log_path = harness.log_path
    events = _read_events(log_path)

    # Expected sequence: user_message → ... → tool_call → tool_result → assistant_message
    assert len(_of(events, UserMessage)) == 1
    assert len(_of(events, ToolCall)) == 1
    assert len(_of(events, ToolResult)) == 1
    assert len(_of(events, AssistantMessage)) == 1
    assert len(_of(events, TurnComplete)) == 1

    # Verify tool_call captured name + input
    tool_call = _of(events, ToolCall)[0]
    assert tool_call.name == "exec"
    assert "async def main" in tool_call.input["source"]

    # Verify tool_result content is the formatted result
    tool_result = _of(events, ToolResult)[0]
    assert "hello" in tool_result.content

    # tool_call/tool_result reference the tool-use request; assistant references both.
    assert tool_call.tool_use_id == tool_result.tool_use_id
    assert len(_of(events, LLMRequest)) == 2

    # Verify wire events include tool events
    messages = [json.loads(m) for m in ws.messages]
    types = [m["type"] for m in messages]
    assert "tool_call" in types
    assert "tool_result" in types
    assert "turn_complete" in types


@pytest.mark.asyncio
async def test_usage_accumulates_across_tool_iterations(tmp_path):
    """Token usage accumulates across multiple LLM requests in a tool turn."""
    import sys

    from archie_agent.llm._types import ToolUseEvent, ToolUseStart

    harness = _make_harness(
        tmp_path,
        responses=[
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(
                    tool_use_id="tu_1",
                    name="exec",
                    input={"source": "async def main(): return 1\n"},
                ),
                Usage(input_tokens=100, output_tokens=20),
                Done(stop_reason="tool_use"),
            ],
            [
                TextDelta(text="done"),
                Usage(input_tokens=200, output_tokens=10),
                Done(stop_reason="end_turn"),
            ],
        ],
        exec_python=sys.executable,
        exec_run_root=tmp_path / "runs",
    )

    await harness.handle_message("test")

    # Total should be sum of both requests
    assert harness.session.total_input_tokens == 300
    assert harness.session.total_output_tokens == 30
