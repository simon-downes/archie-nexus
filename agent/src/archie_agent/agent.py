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

from archie_shared.events import (
    TextDeltaEvent,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
    serialize_event,
)
from archie_shared.models import ModelInfo, calculate_cost
from starlette.websockets import WebSocket

from archie_agent.llm._types import Done, StreamEvent, TextDelta, Usage
from archie_agent.session import Session, TurnLog

log = logging.getLogger(__name__)

# Sentinel pushed to the queue when the worker thread exits (normal or interrupt)
_SENTINEL = None


class AgentLoop:
    """Orchestrates LLM calls and broadcasts events to connected clients.

    Attributes:
        session: Conversation state and persistence.
        clients: Set of connected WebSocket instances for fan-out.
    """

    def __init__(
        self,
        session: Session,
        llm_client: object,
        model_info: ModelInfo,
        system_prompt: str,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._model_info = model_info
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

                if event is _SENTINEL:
                    # Check if this was an interrupt
                    if self._interrupt.is_set():
                        interrupted = True
                        await self.broadcast(TurnInterrupted(turn_index=turn_index))
                    break

                # Translate internal events → wire events and broadcast
                if isinstance(event, TextDelta):
                    assistant_text += event.text
                    await self.broadcast(TextDeltaEvent(turn_index=turn_index, text=event.text))

                elif isinstance(event, Usage):
                    usage_event = event
                    calculate_cost(
                        self._model_info,
                        event.input_tokens,
                        event.output_tokens,
                        event.cache_read_input_tokens,
                        event.cache_write_input_tokens,
                    )
                    # Update session totals
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
                        )
                    )

                elif isinstance(event, Done):
                    # Add assistant turn to session
                    self.session.add_turn(
                        role="assistant",
                        content=assistant_text,
                        turn_index=turn_index,
                        input_tokens=usage_event.input_tokens if usage_event else 0,
                        output_tokens=usage_event.output_tokens if usage_event else 0,
                    )
                    await self.broadcast(
                        TurnComplete(turn_index=turn_index, stop_reason=event.stop_reason)
                    )
                    break

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
                    cache_read_tokens=(
                        usage_event.cache_read_input_tokens if usage_event else 0
                    ),
                    cache_write_tokens=(
                        usage_event.cache_write_input_tokens if usage_event else 0
                    ),
                    model=self._llm.model_id,
                    interrupted=interrupted,
                )
                self.session.flush_turn(turn_log)

            # Wait for thread to finish
            thread.join(timeout=5.0)

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

        except Exception:
            log.exception("Stream worker error")
            # Push error as a TurnError-like signal — the drain loop will handle it
            # We use the sentinel to signal completion; the drain loop checks interrupt
            # For unrecoverable errors, we set interrupt so the drain loop emits TurnError
            self._interrupt.set()

        finally:
            # Always push sentinel to unblock the drain loop
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    async def broadcast(self, event) -> None:
        """Serialize and send an event to all connected WebSocket clients."""
        data = serialize_event(event)
        disconnected = set()
        for ws in self.clients:
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.add(ws)
        self.clients -= disconnected
