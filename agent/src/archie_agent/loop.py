"""Pure agent loop — async generator that yields AgentEvents.

This module owns the sync→async thread bridge for the LLM client. It has NO
knowledge of WebSockets, persistence, session state, or Starlette.

The single public function `run_loop()` is an async generator that:
1. Spawns a sync worker thread to consume the LLM generator
2. Bridges events via asyncio.Queue + call_soon_threadsafe
3. Translates provider-specific StreamEvents → provider-neutral AgentEvents
4. Respects an interrupt signal to cancel mid-stream
"""

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from archie_agent.events import (
    AgentEvent,
    TextChunk,
    TurnDone,
    TurnFailed,
    TurnInterrupted,
    TurnUsage,
)
from archie_agent.llm._types import Done, StreamEvent, TextDelta, Usage

if TYPE_CHECKING:
    from archie_agent.llm import LLMClient
    from archie_agent.session import Turn

log = logging.getLogger(__name__)

# Sentinels pushed onto the queue by the worker thread
_SENTINEL_DONE = object()


@dataclass
class _WorkerError:
    """Carries an error message from the worker thread across the queue boundary."""

    msg: str


async def run_loop(
    *,
    messages: "list[Turn]",
    system: str,
    llm: "LLMClient",
    interrupt: threading.Event,
) -> AsyncGenerator[AgentEvent]:
    """Yield AgentEvents by streaming from the LLM client.

    This is a pure async generator — it does not mutate `messages`, access any
    external state, or perform I/O beyond the LLM call (which happens in the
    worker thread).

    Args:
        messages: Conversation history (live reference, passed to llm.stream()).
        system: System prompt.
        llm: An LLM client implementing the LLMClient protocol.
        interrupt: Threading event — when set, the worker closes the LLM generator
                   and the loop yields TurnInterrupted.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[StreamEvent | _WorkerError | object] = asyncio.Queue()

    def _worker() -> None:
        """Sync thread: consume LLM generator, push events to async queue."""
        gen: Generator[StreamEvent] | None = None
        try:
            gen = llm.stream(messages=messages, system=system, tool_config=None)
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

    # Drain the queue, translating StreamEvent → AgentEvent
    stop_reason: str | None = None
    pending_usage: TurnUsage | None = None
    terminated = False  # True if we yielded TurnFailed or TurnInterrupted

    try:
        while True:
            event = await queue.get()

            # Sentinel: worker is done — could be normal completion or interrupt
            if event is _SENTINEL_DONE:
                # If interrupt was set, the worker exited early
                if interrupt.is_set():
                    terminated = True
                    yield TurnInterrupted()
                break

            # Worker error: yield TurnFailed and stop
            if isinstance(event, _WorkerError):
                terminated = True
                yield TurnFailed(error=event.msg)
                # Drain remaining items until sentinel
                while True:
                    remaining = await queue.get()
                    if remaining is _SENTINEL_DONE:
                        break
                break

            # Check interrupt between processing events
            if interrupt.is_set():
                terminated = True
                yield TurnInterrupted()
                # Drain until sentinel
                while True:
                    remaining = await queue.get()
                    if remaining is _SENTINEL_DONE:
                        break
                break

            # Translate StreamEvent → AgentEvent
            if isinstance(event, TextDelta):
                yield TextChunk(text=event.text)

            elif isinstance(event, Usage):
                # Buffer usage — will be yielded after drain completes
                pending_usage = TurnUsage(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cache_read_tokens=event.cache_read_input_tokens,
                    cache_write_tokens=event.cache_write_input_tokens,
                )

            elif isinstance(event, Done):
                # Record stop reason, do NOT break — continue draining until sentinel
                stop_reason = event.stop_reason

            else:
                # Unknown event type (ToolUseStart, ToolUseEvent, etc.)
                log.debug("Ignoring unhandled StreamEvent: %s", type(event).__name__)

    finally:
        # Yield buffered events on normal completion only.
        # NOTE: yield in finally of an async generator works correctly when the
        # consumer exhausts the generator via `async for`. If the consumer calls
        # aclose() or throws GeneratorExit, these yields are silently skipped —
        # acceptable since that's an abnormal teardown path.
        if not terminated:
            if pending_usage is not None:
                yield pending_usage
            if stop_reason is not None:
                yield TurnDone(stop_reason=stop_reason)

        # Wait for thread to finish
        thread.join(timeout=5.0)
        if thread.is_alive():
            log.warning(
                "Stream worker thread did not exit within 5s (likely blocked on network I/O)"
            )
