"""Agent harness — stateful orchestrator that consumes the pure run loop.

The harness owns:
- Session state (in-memory transcript)
- Turn lifecycle (_turn_active flag, interrupt)
- Persistence (writes per-message JSONL entries)
- Wire event translation and broadcast to connected WebSocket clients

It replaces the old AgentLoop class with a clean separation:
pure loop (loop.py) yields AgentEvents → harness translates to wire events.
"""

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from archie_shared.events import (
    TextDeltaEvent,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
    serialize_event,
)
from archie_shared.models import calculate_cost
from archie_shared.session.log import MessageEntry, MessageMetadata, write_entry
from starlette.websockets import WebSocket
from ulid import ULID

from archie_agent.events import (
    TextChunk,
    TurnDone,
    TurnFailed,
    TurnUsage,
)
from archie_agent.events import (
    # Aliased: avoids collision with archie_shared.events.TurnInterrupted (wire event)
    TurnInterrupted as AgentTurnInterrupted,
)
from archie_agent.loop import run_loop
from archie_agent.session import Session

if TYPE_CHECKING:
    from archie_agent.llm import LLMClient

log = logging.getLogger(__name__)


class AgentHarness:
    """Stateful orchestrator — consumes AgentEvents and manages I/O.

    Same external surface as the old AgentLoop for app.py compatibility:
    - session attribute
    - clients set
    - turn_active property
    - handle_message(content)
    - interrupt()
    """

    def __init__(
        self,
        session: Session,
        llm_client: "LLMClient",
        system_prompt: str,
        log_dir: Path,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._system_prompt = system_prompt
        self._log_dir = log_dir

        # Connected WebSocket clients for broadcast
        self.clients: set[WebSocket] = set()

        # Turn state
        self._turn_active = False
        self._interrupt = threading.Event()

    @property
    def log_path(self) -> Path:
        """Path to the JSONL log file for this session."""
        return self._log_dir / f"{self.session.session_id}.jsonl"

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
            await self._broadcast(TurnError(turn_index=turn_index, message="Turn already active"))
            return

        self._turn_active = True
        turn_index = self.session.next_turn_index()

        # Persist user message (crash-safe: before streaming starts)
        self._persist_message(role="user", content=content)

        # Add user message to in-memory transcript
        self.session.add_turn(role="user", content=content, turn_index=turn_index)

        # Run the pure loop and consume events
        assistant_text = ""
        last_usage: TurnUsage | None = None

        try:
            gen = run_loop(
                messages=self.session.turns,
                system=self._system_prompt,
                llm=self._llm,
                interrupt=self._interrupt,
            )

            async for event in gen:
                if isinstance(event, TextChunk):
                    assistant_text += event.text
                    await self._broadcast(TextDeltaEvent(turn_index=turn_index, text=event.text))

                elif isinstance(event, TurnUsage):
                    last_usage = event
                    # Update session accumulators (including _last_input_tokens)
                    self.session.record_usage(
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        cache_read_tokens=event.cache_read_tokens,
                        cache_write_tokens=event.cache_write_tokens,
                    )
                    await self._broadcast(
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

                elif isinstance(event, TurnDone):
                    # Add assistant turn to transcript (with output_tokens for context_pct)
                    self.session.add_turn(
                        role="assistant",
                        content=assistant_text,
                        turn_index=turn_index,
                        output_tokens=last_usage.output_tokens if last_usage else 0,
                    )
                    # Persist assistant message with per-message cost
                    self._persist_assistant(
                        content=assistant_text,
                        usage=last_usage,
                        interrupted=False,
                    )
                    await self._broadcast(
                        TurnComplete(turn_index=turn_index, stop_reason=event.stop_reason)
                    )

                elif isinstance(event, TurnFailed):
                    # Persist partial text if any accumulated before the error
                    if assistant_text:
                        self.session.add_turn(
                            role="assistant",
                            content=assistant_text,
                            turn_index=turn_index,
                            interrupted=True,
                        )
                        self._persist_assistant(
                            content=assistant_text,
                            usage=last_usage,
                            interrupted=True,
                        )
                    await self._broadcast(TurnError(turn_index=turn_index, message=event.error))

                elif isinstance(event, AgentTurnInterrupted):
                    # Add partial assistant turn (may be empty, with output_tokens for context)
                    self.session.add_turn(
                        role="assistant",
                        content=assistant_text,
                        turn_index=turn_index,
                        output_tokens=last_usage.output_tokens if last_usage else 0,
                        interrupted=True,
                    )
                    # Persist partial assistant message
                    self._persist_assistant(
                        content=assistant_text,
                        usage=last_usage,
                        interrupted=True,
                    )
                    await self._broadcast(TurnInterrupted(turn_index=turn_index))

        except Exception as e:
            log.exception("Error in harness event consumption")
            await self._broadcast(TurnError(turn_index=turn_index, message=str(e)))

        finally:
            self._turn_active = False

    def interrupt(self) -> None:
        """Signal the current turn to stop. Called from the WS read task."""
        self._interrupt.set()

    def _persist_message(self, role: str, content: str) -> None:
        """Persist a message entry to the JSONL log (best-effort)."""
        try:
            entry = MessageEntry(
                id=str(ULID()),
                when=datetime.now(UTC).isoformat(),
                role=role,
                content=content,
            )
            write_entry(self.log_path, entry)
        except Exception:
            log.warning("Failed to persist %s message", role, exc_info=True)

    def _persist_assistant(
        self,
        content: str,
        usage: TurnUsage | None,
        interrupted: bool,
    ) -> None:
        """Persist an assistant message with metadata (best-effort)."""
        try:
            # Compute per-message cost from this turn's tokens
            if usage:
                cost = calculate_cost(
                    self.session.model.cost,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                )
                metadata = MessageMetadata(
                    model=self.session.model_id,
                    backend=self.session.model.provider.name,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost=round(cost, 6),
                    interrupted=interrupted,
                )
            else:
                metadata = MessageMetadata(
                    model=self.session.model_id,
                    backend=self.session.model.provider.name,
                    interrupted=interrupted,
                )

            entry = MessageEntry(
                id=str(ULID()),
                when=datetime.now(UTC).isoformat(),
                role="assistant",
                content=content,
                metadata=metadata,
            )
            write_entry(self.log_path, entry)
        except Exception:
            log.warning("Failed to persist assistant message", exc_info=True)

    async def _broadcast(self, event) -> None:
        """Serialize and send an event to all connected WebSocket clients."""
        data = serialize_event(event)
        disconnected = set()
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.add(ws)
        self.clients -= disconnected
