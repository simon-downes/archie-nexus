"""Pure agent loop — async generator that yields AgentEvents.

This module owns the sync→async thread bridge for the LLM client. It has NO
knowledge of WebSockets, persistence, session state, or Starlette.

The single public function `run_loop()` is an async generator that drives
multi-turn tool loops: stream LLM → if tool_use → execute tools → append
results → re-invoke → repeat until terminal or iteration cap.
"""

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock

from archie_agent.events import (
    AgentEvent,
    IterationStart,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    Usage,
)
from archie_agent.llm._types import (
    Done,
    StreamEvent,
    ToolUseEvent,
    ToolUseStart,
)
from archie_agent.llm._types import (
    TextDelta as LLMTextDelta,
)
from archie_agent.llm._types import (
    Usage as LLMUsage,
)

if TYPE_CHECKING:
    from archie_agent.llm import LLMClient
    from archie_agent.session import Turn

log = logging.getLogger(__name__)

# Sentinels pushed onto the queue by the worker thread
_SENTINEL_DONE = object()

# Default iteration cap for the tool loop
_DEFAULT_MAX_ITERATIONS = 25


@dataclass
class _WorkerError:
    """Carries an error message from the worker thread across the queue boundary."""

    msg: str


@dataclass
class _RequestResult:
    """Result of a single LLM request (inner stream helper).

    Used by the outer loop to decide whether to continue tool looping.
    """

    text_blocks: list[TextBlock] = field(default_factory=list)
    tool_use_blocks: list[ToolUseBlock] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage | None = None
    interrupted: bool = False
    failed: bool = False
    error_msg: str | None = None


async def run_loop(
    *,
    messages: "list[Turn]",
    system: str,
    llm: "LLMClient",
    interrupt: threading.Event,
    tool_config: list[dict] | None = None,
    execute_tool: Callable[[ToolUseBlock], Awaitable[ToolResultBlock]] | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
) -> AsyncGenerator[AgentEvent]:
    """Yield AgentEvents by streaming from the LLM client in a tool loop.

    This is a pure async generator — it does not mutate the caller's `messages`
    list, access any external state, or perform I/O beyond the LLM call and the
    injected execute_tool callable.

    Args:
        messages: Conversation history (NOT mutated — copied internally).
        system: System prompt.
        llm: An LLM client implementing the LLMClient protocol.
        interrupt: Threading event — when set, the loop terminates.
        tool_config: Neutral tool configs to pass to the LLM.
        execute_tool: Async callable to execute a tool_use block. If None,
            tool_use stop reasons are treated as terminal.
        max_iterations: Safety cap on tool loop iterations.
    """
    # Copy messages so we don't mutate the caller's list
    working_messages: list[Turn] = list(messages)

    for _iteration in range(max_iterations):
        # Signal the start of a new iteration so the client can open a fresh
        # visual block deterministically (decoupled from usage metadata).
        yield IterationStart(index=_iteration)
        result = _RequestResult()

        # Stream a single LLM request
        async for event in _stream_once(
            messages=working_messages,
            system=system,
            llm=llm,
            interrupt=interrupt,
            tool_config=tool_config,
            result=result,
        ):
            yield event

        # Handle failure/interrupt — stop the loop
        if result.failed:
            yield TurnError(error=result.error_msg or "unknown error")
            return

        if result.interrupted:
            # If we have pending tool_use blocks, repair history
            if result.tool_use_blocks and execute_tool:
                # All tool_use blocks are orphaned — synthesise error results
                from archie_agent.session import Turn as TurnType

                repair_results = [
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content="cancelled",
                        is_error=True,
                    )
                    for block in result.tool_use_blocks
                ]
                working_messages.append(
                    TurnType(role="assistant", content=result.text_blocks + result.tool_use_blocks)
                )
                working_messages.append(TurnType(role="user", content=repair_results))
            yield TurnInterrupted()
            return

        # Decide: tool loop or terminal?
        has_tool_use = bool(result.tool_use_blocks)
        is_tool_use_stop = result.stop_reason == "tool_use"

        # Emit per-request usage (billing). May be absent when the provider
        # omits usage metadata on a tool-use response; block boundaries are
        # handled separately via IterationStart.
        if result.usage is not None:
            yield result.usage

        if has_tool_use and is_tool_use_stop and execute_tool:
            # --- Tool execution phase ---
            from archie_agent.session import Turn as TurnType

            # Append assistant turn (text + tool_use blocks)
            working_messages.append(
                TurnType(role="assistant", content=result.text_blocks + result.tool_use_blocks)
            )

            # Yield ToolCall events
            for block in result.tool_use_blocks:
                yield ToolCall(
                    tool_use_id=block.tool_use_id,
                    name=block.name,
                    input=block.input,
                )

            # Execute tools, collecting results
            tool_results: list[ToolResultBlock] = []
            for block in result.tool_use_blocks:
                if interrupt.is_set():
                    # Interrupt during tool execution — repair remaining
                    tool_results.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content="cancelled",
                            is_error=True,
                        )
                    )
                    # Also cancel any remaining blocks
                    remaining_idx = result.tool_use_blocks.index(block) + 1
                    for remaining_block in result.tool_use_blocks[remaining_idx:]:
                        tool_results.append(
                            ToolResultBlock(
                                tool_use_id=remaining_block.tool_use_id,
                                content="cancelled",
                                is_error=True,
                            )
                        )
                    working_messages.append(TurnType(role="user", content=tool_results))
                    yield TurnInterrupted()
                    return

                try:
                    result_block = await execute_tool(block)
                except Exception as e:
                    log.warning("execute_tool raised for %s: %s", block.name, e)
                    result_block = ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=f"{type(e).__name__}: {e}",
                        is_error=True,
                    )

                tool_results.append(result_block)

                # Yield ToolResult event
                yield ToolResult(
                    tool_use_id=result_block.tool_use_id,
                    content=result_block.content,
                    is_error=result_block.is_error,
                )

            # Append batched tool results as a single user turn
            working_messages.append(TurnType(role="user", content=tool_results))

            # Continue the loop — re-invoke the LLM
            continue

        # --- Terminal: no more tools or max_tokens without tools ---
        yield TurnComplete(stop_reason=result.stop_reason or "end_turn")
        return

    # Iteration cap hit
    yield TurnError(error="max tool iterations")


async def _stream_once(
    *,
    messages: "list[Turn]",
    system: str,
    llm: "LLMClient",
    interrupt: threading.Event,
    tool_config: list[dict] | None,
    result: _RequestResult,
) -> AsyncGenerator[AgentEvent]:
    """Stream a single LLM request.

    Populates `result` with accumulated data and yields text events.
    Does NOT yield Usage, TurnComplete, TurnError, or TurnInterrupted.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[StreamEvent | _WorkerError | object] = asyncio.Queue()

    def _worker() -> None:
        gen: Generator[StreamEvent] | None = None
        try:
            gen = llm.stream(messages=messages, system=system, tool_config=tool_config)
            for event in gen:
                if interrupt.is_set():
                    gen.close()
                    break
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as e:
            log.exception("Stream worker error")
            loop.call_soon_threadsafe(queue.put_nowait, _WorkerError(f"{type(e).__name__}: {e}"))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL_DONE)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    # State for tool_use block accumulation
    current_tool_use_id: str = ""
    current_tool_name: str = ""

    try:
        while True:
            event = await queue.get()

            if event is _SENTINEL_DONE:
                if interrupt.is_set():
                    result.interrupted = True
                break

            if isinstance(event, _WorkerError):
                result.failed = True
                result.error_msg = event.msg
                # Drain until sentinel
                while True:
                    remaining = await queue.get()
                    if remaining is _SENTINEL_DONE:
                        break
                break

            if interrupt.is_set():
                result.interrupted = True
                while True:
                    remaining = await queue.get()
                    if remaining is _SENTINEL_DONE:
                        break
                break

            # Translate StreamEvent
            if isinstance(event, LLMTextDelta):
                result.text_blocks.append(TextBlock(text=event.text))
                yield TextDelta(text=event.text)

            elif isinstance(event, ToolUseStart):
                current_tool_use_id = event.tool_use_id
                current_tool_name = event.name

            elif isinstance(event, ToolUseEvent):
                result.tool_use_blocks.append(
                    ToolUseBlock(
                        tool_use_id=event.tool_use_id or current_tool_use_id,
                        name=event.name or current_tool_name,
                        input=event.input,
                    )
                )

            elif isinstance(event, LLMUsage):
                result.usage = Usage(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cache_read_tokens=event.cache_read_input_tokens,
                    cache_write_tokens=event.cache_write_input_tokens,
                )

            elif isinstance(event, Done):
                result.stop_reason = event.stop_reason

    finally:
        thread.join(timeout=5.0)
        if thread.is_alive():
            log.warning(
                "Stream worker thread did not exit within 5s (likely blocked on network I/O)"
            )
