"""Integration tests for the AgentLoop with mocked Bedrock."""

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from archie_agent.agent import AgentLoop
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.session import Session
from archie_shared.events import (
    TextDeltaEvent,
    TurnComplete,
    TurnInterrupted,
    UsageUpdated,
    deserialize_event,
)
from archie_shared.models import DEFAULT_MODELS, get_model


def _make_mock_llm(events: list):
    """Create a mock LLM client that yields the given events from stream()."""
    mock = MagicMock()
    mock.model_id = "eu.anthropic.claude-sonnet-4-6"

    def stream(messages, system, tool_config=None):
        yield from events

    mock.stream = stream
    return mock


def _make_mock_llm_slow(events: list, delay: float = 0.1):
    """Create a mock LLM client that yields events slowly (for interrupt testing)."""
    mock = MagicMock()
    mock.model_id = "eu.anthropic.claude-sonnet-4-6"

    def stream(messages, system, tool_config=None):
        for event in events:
            time.sleep(delay)
            yield event

    mock.stream = stream
    return mock


class FakeWebSocket:
    """Minimal WebSocket mock that captures sent messages."""

    def __init__(self):
        self.messages: list[str] = []

    async def send_text(self, data: str) -> None:
        self.messages.append(data)


@pytest.fixture
def model_entry():
    return get_model(DEFAULT_MODELS, "bedrock-claude-sonnet-4-6")


@pytest.fixture
def session(model_entry):
    return Session(
        model_id="bedrock-claude-sonnet-4-6",
        model=model_entry,
        session_id="test-session",
    )


@pytest.mark.asyncio
async def test_handle_message_emits_correct_events(session, model_entry):
    """Verify events are emitted in correct order: TextDelta, Usage, TurnComplete."""
    mock_events = [
        TextDelta(text="Hello"),
        TextDelta(text=" world"),
        Usage(input_tokens=100, output_tokens=10, cache_read_input_tokens=50),
        Done(stop_reason="end_turn"),
    ]

    mock_llm = _make_mock_llm(mock_events)
    agent = AgentLoop(
        session=session,
        llm_client=mock_llm,
        model=model_entry,
        system_prompt="You are helpful.",
    )

    ws = FakeWebSocket()
    agent.clients.add(ws)

    await agent.handle_message("hi")

    # Deserialize and check event order
    events = [deserialize_event(m) for m in ws.messages]

    assert isinstance(events[0], TextDeltaEvent)
    assert events[0].text == "Hello"
    assert events[0].turn_index == 1

    assert isinstance(events[1], TextDeltaEvent)
    assert events[1].text == " world"

    assert isinstance(events[2], UsageUpdated)
    assert events[2].input_tokens == 100
    assert events[2].output_tokens == 10

    assert isinstance(events[3], TurnComplete)
    assert events[3].stop_reason == "end_turn"
    assert events[3].turn_index == 1

    # Session should have 2 turns: user + assistant
    assert len(session.turns) == 2
    assert session.turns[0].role == "user"
    assert session.turns[1].role == "assistant"
    assert session.turns[1].text == "Hello world"


@pytest.mark.asyncio
async def test_interrupt_mid_stream(session, model_entry):
    """Verify interrupt stops the stream and emits TurnInterrupted.

    Uses a slow-yielding mock so the interrupt lands mid-stream, exercising the
    thread/queue/flag path.
    """
    # Many text deltas with delays — gives time to interrupt
    mock_events = [
        TextDelta(text="chunk1"),
        TextDelta(text="chunk2"),
        TextDelta(text="chunk3"),
        TextDelta(text="chunk4"),
        TextDelta(text="chunk5"),
        Usage(input_tokens=50, output_tokens=5),
        Done(stop_reason="end_turn"),
    ]

    mock_llm = _make_mock_llm_slow(mock_events, delay=0.05)
    agent = AgentLoop(
        session=session,
        llm_client=mock_llm,
        model=model_entry,
        system_prompt="You are helpful.",
    )

    ws = FakeWebSocket()
    agent.clients.add(ws)

    # Start the turn in a task
    task = asyncio.create_task(agent.handle_message("hi"))

    # Wait a bit then interrupt
    await asyncio.sleep(0.08)
    agent.interrupt()

    await task

    # Should have received some text deltas then TurnInterrupted
    events = [deserialize_event(m) for m in ws.messages]
    assert any(isinstance(e, TextDeltaEvent) for e in events)
    assert isinstance(events[-1], TurnInterrupted)
    assert events[-1].turn_index == 1

    # Turn should no longer be active
    assert not agent.turn_active


@pytest.mark.asyncio
async def test_no_stale_interrupt_on_next_turn(session, model_entry):
    """Verify interrupt flag from previous turn doesn't leak into next turn."""
    mock_events = [
        TextDelta(text="Hello"),
        Usage(input_tokens=50, output_tokens=5),
        Done(stop_reason="end_turn"),
    ]

    mock_llm = _make_mock_llm(mock_events)
    agent = AgentLoop(
        session=session,
        llm_client=mock_llm,
        model=model_entry,
        system_prompt="You are helpful.",
    )

    ws = FakeWebSocket()
    agent.clients.add(ws)

    # Manually set interrupt as if from a previous turn
    agent._interrupt.set()

    # handle_message should clear it and proceed normally
    await agent.handle_message("hi")

    events = [deserialize_event(m) for m in ws.messages]
    # Should complete normally, not be interrupted
    assert isinstance(events[-1], TurnComplete)
