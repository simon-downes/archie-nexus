"""Tests for the pure run_loop async generator."""

import threading

import pytest
from archie_agent.events import (
    IterationStart,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
)
from archie_agent.events import (
    TextDelta as AgentTextDelta,
)
from archie_agent.events import (
    Usage as AgentUsage,
)
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.loop import run_loop
from archie_agent.session import Turn
from archie_shared.types import TextBlock, ToolResultBlock

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
        IterationStart(index=0),
        AgentTextDelta(text="Hello"),
        AgentTextDelta(text=" world"),
        AgentUsage(input_tokens=100, output_tokens=10, cache_read_tokens=5, cache_write_tokens=2),
        TurnComplete(stop_reason="end_turn"),
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
    assert events[-2] == AgentUsage(input_tokens=50, output_tokens=5)
    assert events[-1] == TurnComplete(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_empty_response():
    """Done without any TextDelta is valid — yields TurnDone only."""
    llm = FakeLLMClient(responses=[[Done(stop_reason="end_turn")]])
    interrupt = threading.Event()

    gen = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events = await _collect(gen)

    assert events == [IterationStart(index=0), TurnComplete(stop_reason="end_turn")]


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
        if isinstance(event, AgentTextDelta) and event.text == "chunk1":
            # Set interrupt after first chunk
            interrupt.set()

    # Should have chunk1, then TurnInterrupted (no TurnDone)
    assert events[0] == IterationStart(index=0)
    assert events[1] == AgentTextDelta(text="chunk1")
    assert any(isinstance(e, TurnInterrupted) for e in events)
    assert not any(isinstance(e, TurnComplete) for e in events)


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
    assert not any(isinstance(e, AgentTextDelta) for e in events)


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

    assert len(events) == 2
    assert events[0] == IterationStart(index=0)
    assert isinstance(events[1], TurnError)
    assert "RuntimeError: Connection refused" in events[1].error


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
    assert events1[0] == IterationStart(index=0)
    assert events1[1] == AgentTextDelta(text="first")

    interrupt.clear()
    gen2 = run_loop(messages=_make_messages(), system="test", llm=llm, interrupt=interrupt)
    events2 = await _collect(gen2)
    assert events2[0] == IterationStart(index=0)
    assert events2[1] == AgentTextDelta(text="second")


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
    assert events[0] == IterationStart(index=0)
    assert events[1] == AgentTextDelta(text="partial")
    assert isinstance(events[-1], TurnError)
    assert "RuntimeError" in events[-1].error


# --- Tool loop tests ---


@pytest.mark.asyncio
async def test_single_tool_round_trip():
    """Model calls one tool, gets result, then answers with text."""
    llm = FakeLLMClient(
        responses=[
            # First response: tool_use
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(tool_use_id="tu_1", name="exec", input={"source": "code"}),
                Usage(input_tokens=100, output_tokens=20),
                Done(stop_reason="tool_use"),
            ],
            # Second response: text answer
            [
                TextDelta(text="The answer is 42"),
                Usage(input_tokens=200, output_tokens=15),
                Done(stop_reason="end_turn"),
            ],
        ]
    )
    interrupt = threading.Event()

    async def execute_tool(block):
        assert block.name == "exec"
        return ToolResultBlock(tool_use_id=block.tool_use_id, content="return: 42")

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
    )
    events = await _collect(gen)

    # Expected: AgentUsage(first), ToolCall, ToolResult, AgentUsage(second), TextChunk, TurnDone
    usage_events = [e for e in events if isinstance(e, AgentUsage)]
    assert len(usage_events) == 2
    assert usage_events[0].input_tokens == 100
    assert usage_events[1].input_tokens == 200

    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "exec"
    assert tool_calls[0].input == {"source": "code"}

    tool_results = [e for e in events if isinstance(e, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].content == "return: 42"
    assert not tool_results[0].is_error

    assert events[-1] == TurnComplete(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_iteration_start_emitted_per_iteration():
    """Regression (Bug 2): each tool-loop iteration begins with an IterationStart
    event so the client can open a fresh visual block deterministically, even
    when a tool-use response omits Usage metadata. Two tool iterations followed
    by a terminal text answer must yield three IterationStart events, each before
    that iteration's content.
    """
    llm = FakeLLMClient(
        responses=[
            # Iteration 0: tool_use WITHOUT Usage metadata
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(tool_use_id="tu_1", name="exec", input={"source": "a"}),
                Done(stop_reason="tool_use"),
            ],
            # Iteration 1: another tool_use WITHOUT Usage metadata
            [
                ToolUseStart(tool_use_id="tu_2", name="exec"),
                ToolUseEvent(tool_use_id="tu_2", name="exec", input={"source": "b"}),
                Done(stop_reason="tool_use"),
            ],
            # Iteration 2: terminal text answer
            [
                TextDelta(text="done"),
                Usage(input_tokens=50, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        ]
    )
    interrupt = threading.Event()

    async def execute_tool(block):
        return ToolResultBlock(tool_use_id=block.tool_use_id, content="ok")

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
    )
    events = await _collect(gen)

    # Three iterations → three IterationStart events with sequential indices.
    starts = [e for e in events if isinstance(e, IterationStart)]
    assert [e.index for e in starts] == [0, 1, 2]

    # Each ToolCall must be preceded by an IterationStart (block boundary).
    types = [type(e).__name__ for e in events]
    first_toolcall = types.index("ToolCall")
    assert "IterationStart" in types[:first_toolcall]


@pytest.mark.asyncio
async def test_two_tools_batched():
    """Model calls two tools in one response, both are executed and batched."""
    llm = FakeLLMClient(
        responses=[
            # First response: two tool_use blocks
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(tool_use_id="tu_1", name="exec", input={"source": "a"}),
                ToolUseStart(tool_use_id="tu_2", name="exec"),
                ToolUseEvent(tool_use_id="tu_2", name="exec", input={"source": "b"}),
                Usage(input_tokens=100, output_tokens=30),
                Done(stop_reason="tool_use"),
            ],
            # Second response: text answer
            [
                TextDelta(text="Done"),
                Usage(input_tokens=300, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        ]
    )
    interrupt = threading.Event()
    executed = []

    async def execute_tool(block):
        executed.append(block.tool_use_id)
        return ToolResultBlock(tool_use_id=block.tool_use_id, content=f"result_{block.tool_use_id}")

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
    )
    events = await _collect(gen)

    assert executed == ["tu_1", "tu_2"]

    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(tool_calls) == 2

    tool_results = [e for e in events if isinstance(e, ToolResult)]
    assert len(tool_results) == 2

    assert events[-1] == TurnComplete(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_iteration_cap():
    """Exceeding max_iterations yields TurnFailed."""
    # LLM always requests tools
    responses = [
        [
            ToolUseStart(tool_use_id=f"tu_{i}", name="exec"),
            ToolUseEvent(tool_use_id=f"tu_{i}", name="exec", input={"source": "x"}),
            Usage(input_tokens=10, output_tokens=5),
            Done(stop_reason="tool_use"),
        ]
        for i in range(5)
    ]
    llm = FakeLLMClient(responses=responses)
    interrupt = threading.Event()

    async def execute_tool(block):
        return ToolResultBlock(tool_use_id=block.tool_use_id, content="ok")

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
        max_iterations=3,
    )
    events = await _collect(gen)

    assert events[-1] == TurnError(error="max tool iterations")


@pytest.mark.asyncio
async def test_interrupt_mid_tool_history_repair():
    """Interrupt during tool execution synthesises cancelled results."""
    llm = FakeLLMClient(
        responses=[
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(tool_use_id="tu_1", name="exec", input={"source": "a"}),
                ToolUseStart(tool_use_id="tu_2", name="exec"),
                ToolUseEvent(tool_use_id="tu_2", name="exec", input={"source": "b"}),
                Usage(input_tokens=100, output_tokens=20),
                Done(stop_reason="tool_use"),
            ],
        ]
    )
    interrupt = threading.Event()
    call_count = 0

    async def execute_tool(block):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # After first tool, set interrupt
            interrupt.set()
            return ToolResultBlock(tool_use_id=block.tool_use_id, content="result_1")
        # Should not reach here for second tool — interrupt triggers cancel
        return ToolResultBlock(tool_use_id=block.tool_use_id, content="result_2")

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
    )
    events = await _collect(gen)

    assert any(isinstance(e, TurnInterrupted) for e in events)
    # Only the first tool should have been executed
    assert call_count == 1


@pytest.mark.asyncio
async def test_execute_tool_raises():
    """execute_tool raising produces an error ToolResult and continues."""
    llm = FakeLLMClient(
        responses=[
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(tool_use_id="tu_1", name="exec", input={"source": "x"}),
                Usage(input_tokens=100, output_tokens=10),
                Done(stop_reason="tool_use"),
            ],
            # Model responds to the error
            [
                TextDelta(text="Sorry, error occurred"),
                Usage(input_tokens=200, output_tokens=10),
                Done(stop_reason="end_turn"),
            ],
        ]
    )
    interrupt = threading.Event()

    async def execute_tool(block):
        raise RuntimeError("execution failed")

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
    )
    events = await _collect(gen)

    tool_results = [e for e in events if isinstance(e, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is True
    assert "RuntimeError" in tool_results[0].content

    # Loop continued and model responded
    assert events[-1] == TurnComplete(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_messages_not_mutated_with_tools():
    """Tool loop does not mutate the caller's messages list."""
    messages = _make_messages()
    original_len = len(messages)

    llm = FakeLLMClient(
        responses=[
            [
                ToolUseStart(tool_use_id="tu_1", name="exec"),
                ToolUseEvent(tool_use_id="tu_1", name="exec", input={"source": "x"}),
                Usage(input_tokens=100, output_tokens=10),
                Done(stop_reason="tool_use"),
            ],
            [
                TextDelta(text="done"),
                Usage(input_tokens=200, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        ]
    )
    interrupt = threading.Event()

    async def execute_tool(block):
        return ToolResultBlock(tool_use_id=block.tool_use_id, content="ok")

    gen = run_loop(
        messages=messages,
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=execute_tool,
    )
    await _collect(gen)

    assert len(messages) == original_len


@pytest.mark.asyncio
async def test_max_tokens_without_tool_use_is_terminal():
    """max_tokens stop without tool_use blocks is terminal."""
    llm = FakeLLMClient(
        responses=[
            [
                TextDelta(text="truncated response"),
                Usage(input_tokens=100, output_tokens=4096),
                Done(stop_reason="max_tokens"),
            ],
        ]
    )
    interrupt = threading.Event()

    gen = run_loop(
        messages=_make_messages(),
        system="test",
        llm=llm,
        interrupt=interrupt,
        tool_config=[{"name": "exec", "description": "test", "input_schema": {}}],
        execute_tool=lambda b: None,
    )
    events = await _collect(gen)

    assert events[-1] == TurnComplete(stop_reason="max_tokens")
