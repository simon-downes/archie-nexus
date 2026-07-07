"""Tests for the AgentHarness — full path from message to wire events + persistence."""

import json

import msgspec
import pytest
from archie_agent.harness import AgentHarness
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_shared.models import CostConfig, ModelEntry, ProviderConfig
from archie_shared.session.log import MessageEntry

# --- Fixtures ---


def _make_model() -> ModelEntry:
    """Create a test ModelEntry."""
    return ModelEntry(
        name="Test Model",
        provider=ProviderConfig(name="bedrock", endpoint="test-model-id", region="us-east-1"),
        cost=CostConfig(input=3.0, output=15.0),
        context=200000,
        max_output_tokens=4096,
    )


def _make_harness(
    tmp_path,
    responses: list[list],
    delay: float = 0.0,
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
        system_prompt="You are a test assistant.",
        log_dir=tmp_path,
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
    assert "usage_updated" in event_types
    assert "turn_complete" in event_types

    # Verify session state
    assert harness.session.total_input_tokens == 100
    assert harness.session.total_output_tokens == 10
    assert len(harness.session.turns) == 2  # user + assistant

    # Verify JSONL persistence
    log_path = tmp_path / "test-session-001.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2  # user + assistant

    user_entry = msgspec.json.decode(lines[0], type=MessageEntry)
    assert user_entry.role == "user"
    assert user_entry.content == "Hi there"
    assert user_entry.metadata is None

    assistant_entry = msgspec.json.decode(lines[1], type=MessageEntry)
    assert assistant_entry.role == "assistant"
    assert assistant_entry.content == "Hello world"
    assert assistant_entry.metadata is not None
    assert assistant_entry.metadata.input_tokens == 100
    assert assistant_entry.metadata.output_tokens == 10
    assert assistant_entry.metadata.cost > 0
    assert assistant_entry.metadata.interrupted is False


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

    # Verify persistence: user + partial assistant
    log_path = tmp_path / "test-session-001.jsonl"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2

    assistant_entry = msgspec.json.decode(lines[1], type=MessageEntry)
    assert assistant_entry.role == "assistant"
    assert assistant_entry.metadata.interrupted is True


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
        system_prompt="test",
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

    # Should get a turn_error for the rejected message
    events = ws.parsed_events()
    error_events = [e for e in events if e["type"] == "turn_error"]
    assert len(error_events) >= 1
    assert "already active" in error_events[0]["data"]["message"]

    await task


@pytest.mark.asyncio
async def test_usage_updates_context_pct(tmp_path):
    """TurnUsage updates session._last_input_tokens for context_pct."""
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

    # context_pct should be based on the input_tokens + last turn's output_tokens
    # (50000 + 100 output) / 200000 context * 100 = 25.05%
    assert harness.session.context_pct == pytest.approx(25.05)
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
    lines = log_path.read_text().strip().splitlines()
    # 4 lines: user1, assistant1, user2, assistant2
    assert len(lines) == 4

    a1 = msgspec.json.decode(lines[1], type=MessageEntry)
    a2 = msgspec.json.decode(lines[3], type=MessageEntry)

    # Each should have its own per-message cost, not cumulative
    assert a1.metadata.input_tokens == 100
    assert a2.metadata.input_tokens == 200
    # Second cost should be larger (more tokens)
    assert a2.metadata.cost > a1.metadata.cost
    # Neither should equal session total_cost
    assert a1.metadata.cost != harness.session.total_cost


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
        system_prompt="test",
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

    # Partial text should be persisted
    log_path = tmp_path / "partial-err.jsonl"
    lines = log_path.read_text().strip().splitlines()
    # user + partial assistant
    assert len(lines) == 2

    assistant_entry = msgspec.json.decode(lines[1], type=MessageEntry)
    assert assistant_entry.content == "partial response"
    assert assistant_entry.metadata.interrupted is True

    # Turn should be released
    assert harness.turn_active is False
