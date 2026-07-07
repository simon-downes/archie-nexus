"""Tests for the pure run_loop async generator."""

import threading

import pytest
from archie_agent.events import (
    TextChunk,
    TurnDone,
    TurnFailed,
    TurnInterrupted,
    TurnUsage,
)
from archie_agent.llm._types import Done, TextDelta, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.loop import run_loop
from archie_agent.session import Turn
from archie_shared.types import TextBlock

# --- Helpers ---


def _make_messages() -> list[Turn]:
    """Create a simple user message history."""
    return [Turn(role="user", content=[TextBlock(text="hello")], turn_index=1)]


async def _collect(gen) -> list:
    """Collect all events from an async generator."""
    events = []
    async for event in gen:
        events.append(event)
    return events


# --- Tests ---


@pytest.mark.asyncio
async def test_normal_flow():
    """Text chunks, usage, and done are yielded in correct order."""
    llm = FakeLLMClient(
        responses=[
            [
                TextDelta(text="Hello"),
                TextDelta(text=" world"),
                Usage(
                    input_tokens=100,
                    output_tokens=10,
                    cache_read_input_tokens=5,
                    cache_write_input_tokens=2,
                ),
                Done(stop_reason="end_turn"),
            ]
        ]
    )
    interrupt = threading.Event()

    gen = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events = await _collect(gen)

    assert events == [
        TextChunk(text="Hello"),
        TextChunk(text=" world"),
        TurnUsage(input_tokens=100, output_tokens=10, cache_read_tokens=5, cache_write_tokens=2),
        TurnDone(stop_reason="end_turn"),
    ]


@pytest.mark.asyncio
async def test_usage_before_done_ordering():
    """Usage emitted before Done still results in TurnUsage before TurnDone."""
    llm = FakeLLMClient(
        responses=[
            [
                TextDelta(text="hi"),
                Usage(input_tokens=50, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        ]
    )
    interrupt = threading.Event()

    gen = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events = await _collect(gen)

    # TurnUsage always comes before TurnDone
    assert events[-2] == TurnUsage(input_tokens=50, output_tokens=5)
    assert events[-1] == TurnDone(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_empty_response():
    """Done without any TextDelta is valid — yields TurnDone only."""
    llm = FakeLLMClient(responses=[[Done(stop_reason="end_turn")]])
    interrupt = threading.Event()

    gen = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events = await _collect(gen)

    assert events == [TurnDone(stop_reason="end_turn")]


@pytest.mark.asyncio
async def test_interrupt_mid_stream():
    """Setting interrupt during streaming yields TurnInterrupted."""
    llm = FakeLLMClient(
        responses=[
            [
                TextDelta(text="chunk1"),
                TextDelta(text="chunk2"),
                TextDelta(text="chunk3"),
                Done(stop_reason="end_turn"),
            ]
        ],
        delay=0.05,
    )
    interrupt = threading.Event()

    gen = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)

    events = []
    async for event in gen:
        events.append(event)
        if isinstance(event, TextChunk) and event.text == "chunk1":
            # Set interrupt after first chunk
            interrupt.set()

    # Should have chunk1, then TurnInterrupted (no TurnDone)
    assert events[0] == TextChunk(text="chunk1")
    assert any(isinstance(e, TurnInterrupted) for e in events)
    assert not any(isinstance(e, TurnDone) for e in events)


@pytest.mark.asyncio
async def test_interrupt_before_first_event():
    """Interrupt set before streaming starts yields TurnInterrupted."""
    interrupt = threading.Event()
    interrupt.set()  # Pre-set

    llm = FakeLLMClient(
        responses=[
            [
                TextDelta(text="should not appear"),
                Done(stop_reason="end_turn"),
            ]
        ]
    )

    gen = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events = await _collect(gen)

    # Worker sees interrupt immediately, closes gen, sends sentinel
    # Drain loop sees interrupt on sentinel arrival
    assert any(isinstance(e, TurnInterrupted) for e in events)
    assert not any(isinstance(e, TextChunk) for e in events)


@pytest.mark.asyncio
async def test_worker_error():
    """Exception in LLM client yields TurnFailed with error message."""

    class _ErrorLLM:
        model_id = "test-error"

        def stream(self, messages, system, tool_config=None):
            raise RuntimeError("Connection refused")
            yield  # noqa: RET503

        def invoke(self, messages, system):
            return ""

    interrupt = threading.Event()
    gen = run_loop(messages=_make_messages(), system="test", llm=_ErrorLLM(), interrupt=interrupt)
    events = await _collect(gen)

    assert len(events) == 1
    assert isinstance(events[0], TurnFailed)
    assert "RuntimeError: Connection refused" in events[0].error


@pytest.mark.asyncio
async def test_messages_not_mutated():
    """run_loop does not modify the messages list."""
    messages = _make_messages()
    original_len = len(messages)

    llm = FakeLLMClient(responses=[[TextDelta(text="x"), Done(stop_reason="end_turn")]])
    interrupt = threading.Event()

    gen = run_loop(messages=messages, system="test", llm=llm, interrupt=interrupt)
    await _collect(gen)

    assert len(messages) == original_len


@pytest.mark.asyncio
async def test_multiple_stream_calls():
    """FakeLLMClient serves different responses for sequential stream() calls."""
    llm = FakeLLMClient(
        responses=[
            [TextDelta(text="first"), Done(stop_reason="end_turn")],
            [TextDelta(text="second"), Done(stop_reason="end_turn")],
        ]
    )
    interrupt = threading.Event()

    gen1 = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events1 = await _collect(gen1)
    assert events1[0] == TextChunk(text="first")

    interrupt.clear()
    gen2 = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events2 = await _collect(gen2)
    assert events2[0] == TextChunk(text="second")


@pytest.mark.asyncio
async def test_partial_text_before_error():
    """LLM yields text then raises → TextChunks followed by TurnFailed."""

    class _PartialErrorLLM:
        model_id = "test-partial-error"

        def stream(self, messages, system, tool_config=None):
            yield TextDelta(text="partial")
            raise RuntimeError("Connection lost")

        def invoke(self, messages, system):
            return ""

    interrupt = threading.Event()
    gen = run_loop(
        messages=_make_messages(), system="test", llm=_PartialErrorLLM(), interrupt=interrupt
    )
    events = await _collect(gen)

    # Should get TextChunk then TurnFailed
    assert events[0] == TextChunk(text="partial")
    assert isinstance(events[-1], TurnFailed)
    assert "RuntimeError" in events[-1].error
