"""Agent loop — orchestrates LLM calls and event broadcasting.

Text-only for now (no tool dispatch). The loop:
1. Receive user message
2. Add to session history
3. Stream LLM response in a background thread
4. Translate internal stream events → wire protocol events
5. Broadcast to all connected WebSocket clients
6. On completion, add assistant turn to session and flush to JSONL

Threading model:
- handle_message() is an async method called from the WS handler
- It spawns a synchronous worker thread that reads from the Bedrock generator
- The worker pushes events to the async loop via loop.call_soon_threadsafe
- The drain loop awaits the queue and broadcasts each event
- Interrupt is set by the WS read task (independent of the drain loop)
"""

import asyncio
import logging
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from archie_shared.events import (
    TextDeltaEvent,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
    serialize_event,
)
from archie_shared.models import ModelEntry
from starlette.websockets import WebSocket

from archie_agent.llm._types import Done, StreamEvent, TextDelta, Usage
from archie_agent.session import Session, TurnLog

if TYPE_CHECKING:
    from archie_agent.llm import LLMClient

log = logging.getLogger(__name__)

# Sentinel values pushed to the queue to signal completion
_SENTINEL_DONE = object()  # Normal or interrupt completion
_SENTINEL_ERROR = object()  # Unrecoverable exception in worker


class AgentLoop:
    """Orchestrates LLM calls and broadcasts events to connected clients.

    Attributes:
        session: Conversation state and persistence.
        clients: Set of connected WebSocket instances for fan-out.
    """

    def __init__(
        self,
        session: Session,
        llm_client: "LLMClient",
        model: ModelEntry,
        system_prompt: str,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._model = model
        self._system_prompt = system_prompt

        # Connected WebSocket clients for broadcast
        self.clients: set[WebSocket] = set()

        # Turn state
        self._turn_active = False
        self._interrupt = threading.Event()

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    async def handle_message(self, content: str) -> None:
        """Process a user message: stream LLM response and broadcast events.

        This is the main entry point called by the WebSocket handler.
        Clears the interrupt flag at entry to prevent stale flags leaking.
        """
        # Clear stale interrupt from previous turn
        self._interrupt.clear()
        self._worker_error: str = ""

        if self._turn_active:
            turn_index = self.session.turn_index or 1
            await self.broadcast(TurnError(turn_index=turn_index, message="Turn already active"))
            return

        self._turn_active = True
        turn_index = self.session.next_turn_index()

        # Add user message to session
        self.session.add_turn(role="user", content=content, turn_index=turn_index)

        # Prepare the queue for async/sync bridge
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

        # Spawn background thread to consume the Bedrock generator
        thread = threading.Thread(
            target=self._stream_worker,
            args=(loop, queue),
            daemon=True,
        )
        thread.start()

        # Drain the queue and broadcast events
        assistant_text = ""
        usage_event: Usage | None = None
        interrupted = False

        try:
            while True:
                event = await queue.get()

                if event is _SENTINEL_DONE:
                    # Normal completion or user interrupt
                    if self._interrupt.is_set():
                        interrupted = True
                        await self.broadcast(TurnInterrupted(turn_index=turn_index))
                    break

                if event is _SENTINEL_ERROR:
                    # Unrecoverable exception in the worker thread
                    interrupted = True
                    error_msg = getattr(self, "_worker_error", "LLM request failed")
                    await self.broadcast(TurnError(turn_index=turn_index, message=error_msg))
                    break

                # Translate internal events → wire events and broadcast
                if isinstance(event, TextDelta):
                    assistant_text += event.text
                    await self.broadcast(TextDeltaEvent(turn_index=turn_index, text=event.text))

                elif isinstance(event, Usage):
                    usage_event = event
                    # Update session totals directly (not via add_turn — that's for
                    # the assistant turn at completion). Usage is per-request metadata.
                    self.session.total_input_tokens += event.input_tokens
                    self.session.total_output_tokens += event.output_tokens
                    self.session.total_cache_read_tokens += event.cache_read_input_tokens
                    self.session.total_cache_write_tokens += event.cache_write_input_tokens

                    await self.broadcast(
                        UsageUpdated(
                            turn_index=turn_index,
                            input_tokens=self.session.total_input_tokens,
                            output_tokens=self.session.total_output_tokens,
                            cache_read_tokens=self.session.total_cache_read_tokens,
                            cache_write_tokens=self.session.total_cache_write_tokens,
                            cost=self.session.total_cost,
                            context_pct=self.session.context_pct,
                        )
                    )

                elif isinstance(event, Done):
                    # Record completion. Do NOT break here: Bedrock emits the
                    # `metadata`/usage event AFTER `messageStop` (Done), so the
                    # Usage event is still queued behind this one. Breaking now
                    # would drop token/cost data (both live UsageUpdated and the
                    # JSONL flush). Add the assistant turn now, then keep draining
                    # until the worker's sentinel so the trailing Usage arrives.
                    self.session.add_turn(
                        role="assistant",
                        content=assistant_text,
                        turn_index=turn_index,
                    )
                    await self.broadcast(
                        TurnComplete(turn_index=turn_index, stop_reason=event.stop_reason)
                    )

        except Exception as e:
            log.exception("Error in agent loop drain")
            await self.broadcast(TurnError(turn_index=turn_index, message=str(e)))
            interrupted = True

        finally:
            self._turn_active = False

            # Flush to JSONL
            if assistant_text or interrupted:
                if interrupted and assistant_text:
                    # Repair history: add partial text as assistant turn
                    self.session.add_turn(
                        role="assistant",
                        content=assistant_text,
                        turn_index=turn_index,
                        interrupted=True,
                    )

                turn_log = TurnLog(
                    when=datetime.now(UTC).isoformat(),
                    user=content,
                    assistant_text=assistant_text,
                    input_tokens=usage_event.input_tokens if usage_event else 0,
                    output_tokens=usage_event.output_tokens if usage_event else 0,
                    cache_read_tokens=(usage_event.cache_read_input_tokens if usage_event else 0),
                    cache_write_tokens=(usage_event.cache_write_input_tokens if usage_event else 0),
                    model=self._llm.model_id,
                    interrupted=interrupted,
                )
                self.session.flush_turn(turn_log)

            # Wait for thread to finish
            thread.join(timeout=5.0)
            if thread.is_alive():
                log.warning(
                    "Stream worker thread did not exit within 5s (likely blocked on network I/O)"
                )

    def interrupt(self) -> None:
        """Signal the current turn to stop. Called from the WS read task."""
        self._interrupt.set()

    def _stream_worker(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[StreamEvent | None],
    ) -> None:
        """Background thread: consume Bedrock generator and push events to queue.

        Uses loop.call_soon_threadsafe to safely push to the asyncio Queue.
        Checks interrupt flag between generator yields.
        """
        error = False
        try:
            messages = self.session.turns
            gen: Generator[StreamEvent] = self._llm.stream(
                messages=messages,
                system=self._system_prompt,
            )

            for event in gen:
                if self._interrupt.is_set():
                    # Close the EventStream explicitly to release HTTP connection
                    gen.close()
                    break
                loop.call_soon_threadsafe(queue.put_nowait, event)

        except Exception as e:
            log.exception("Stream worker error")
            error = True
            # Store the error message for the drain loop to include in TurnError
            self._worker_error = f"{type(e).__name__}: {e}"

        finally:
            # Push appropriate sentinel to unblock the drain loop
            sentinel = _SENTINEL_ERROR if error else _SENTINEL_DONE
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    async def broadcast(self, event) -> None:
        """Serialize and send an event to all connected WebSocket clients."""
        data = serialize_event(event)
        disconnected = set()
        # Snapshot to avoid RuntimeError if clients set is mutated during iteration
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.add(ws)
        self.clients -= disconnected
