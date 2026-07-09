"""Agent harness — stateful orchestrator that consumes the pure run loop.

The harness owns:
- Session state (in-memory transcript)
- Turn lifecycle (_turn_active flag, interrupt)
- Tool registry and execution
- Persistence (writes per-message JSONL entries)
- Wire event translation and broadcast to connected WebSocket clients
"""

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from archie_shared.events import (
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
    serialize_event,
)
from archie_shared.models import calculate_cost
from archie_shared.session.log import MessageEntry, MessageMetadata, write_entry
from archie_shared.types import ToolResultBlock, ToolUseBlock
from starlette.websockets import WebSocket
from ulid import ULID

from archie_agent.events import (
    TextChunk,
    ToolCall,
    ToolResult,
    TurnDone,
    TurnFailed,
    TurnUsage,
)
from archie_agent.events import (
    # Aliased: avoids collision with archie_shared.events.TurnInterrupted (wire event)
    TurnInterrupted as AgentTurnInterrupted,
)
from archie_agent.exec.tool import create_registry, format_result, run_exec
from archie_agent.loop import run_loop
from archie_agent.session import DisplayEntry, Session

if TYPE_CHECKING:
    from archie_agent.llm import LLMClient

log = logging.getLogger(__name__)


def _extract_duration_ms(content: str) -> int:
    """Extract duration in ms from formatted result content."""
    for line in content.split("\n"):
        if line.startswith("duration:") and line.endswith("ms"):
            try:
                return int(line.removeprefix("duration:").removesuffix("ms").strip())
            except ValueError:
                pass
    return 0


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
        *,
        exec_python: str | None = None,
        exec_run_root: Path | None = None,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._system_prompt = system_prompt
        self._log_dir = log_dir

        # exec runner overrides (for testing)
        self._exec_python = exec_python
        self._exec_run_root = exec_run_root

        # Connected WebSocket clients for broadcast
        self.clients: set[WebSocket] = set()

        # Turn state
        self._turn_active = False
        self._interrupt = threading.Event()

        # Tool registry
        self._registry = create_registry()
        self._tool_config = self._registry.to_tool_config()

        # Active runner subprocess for cancellation
        self._active_proc: asyncio.subprocess.Process | None = None

    @property
    def log_path(self) -> Path:
        """Path to the JSONL log file for this session."""
        return self._log_dir / f"{self.session.session_id}.jsonl"

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    async def handle_message(self, content: str) -> None:
        """Process a user message: stream LLM response and broadcast events.

        Clears the interrupt flag at entry to prevent stale flags leaking.
        """
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
                tool_config=self._tool_config,
                execute_tool=self._execute_tool,
            )

            async for event in gen:
                if isinstance(event, TextChunk):
                    assistant_text += event.text
                    await self._broadcast(TextDeltaEvent(turn_index=turn_index, text=event.text))

                elif isinstance(event, TurnUsage):
                    last_usage = event
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

                elif isinstance(event, ToolCall):
                    # Persist tool_call entry (JSON-serialised name+source)
                    self._persist_tool_call(event)
                    # Broadcast wire event
                    input_summary = self._summarise_tool_input(event.input)
                    await self._broadcast(
                        ToolCallEvent(
                            turn_index=turn_index,
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input_summary=input_summary,
                        )
                    )

                elif isinstance(event, ToolResult):
                    # Persist tool_result entry
                    self._persist_tool_result(event)
                    # Extract duration from content (format: "duration: NNNms")
                    duration_ms = _extract_duration_ms(event.content)
                    # Broadcast wire event
                    summary = event.content[:200] if event.content else ""
                    await self._broadcast(
                        ToolResultEvent(
                            turn_index=turn_index,
                            tool_use_id=event.tool_use_id,
                            is_error=event.is_error,
                            summary=summary,
                            duration_ms=duration_ms,
                            result_bytes=len(event.content.encode("utf-8")) if event.content else 0,
                        )
                    )

                elif isinstance(event, TurnDone):
                    # Add final assistant turn to transcript
                    self.session.add_turn(
                        role="assistant",
                        content=assistant_text,
                        turn_index=turn_index,
                        output_tokens=last_usage.output_tokens if last_usage else 0,
                    )
                    self._persist_assistant(
                        content=assistant_text,
                        usage=last_usage,
                        interrupted=False,
                    )
                    await self._broadcast(
                        TurnComplete(turn_index=turn_index, stop_reason=event.stop_reason)
                    )

                elif isinstance(event, TurnFailed):
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
                    # Persist and record the error for history replay
                    self._persist_message(role="error", content=event.error)
                    self.session.display_entries.append(
                        DisplayEntry(role="error", content=event.error, turn_index=turn_index)
                    )
                    await self._broadcast(TurnError(turn_index=turn_index, message=event.error))

                elif isinstance(event, AgentTurnInterrupted):
                    # Only add to transcript if there's actual content
                    if assistant_text:
                        self.session.add_turn(
                            role="assistant",
                            content=assistant_text,
                            turn_index=turn_index,
                            output_tokens=last_usage.output_tokens if last_usage else 0,
                            interrupted=True,
                        )
                        self._persist_assistant(
                            content=assistant_text,
                            usage=last_usage,
                            interrupted=True,
                        )
                    # Persist and record the interruption for history replay
                    self._persist_message(role="interrupted", content="")
                    self.session.display_entries.append(
                        DisplayEntry(role="interrupted", content="", turn_index=turn_index)
                    )
                    await self._broadcast(TurnInterrupted(turn_index=turn_index))

        except Exception as e:
            log.exception("Error in harness event consumption")
            await self._broadcast(TurnError(turn_index=turn_index, message=str(e)))

        finally:
            self._turn_active = False

    def interrupt(self) -> None:
        """Signal the current turn to stop. Also cancels any active subprocess."""
        self._interrupt.set()
        self._cancel_active_proc()

    async def _execute_tool(self, block: ToolUseBlock) -> ToolResultBlock:
        """Execute a tool_use block and return a ToolResultBlock.

        Uses the tool registry to find the handler. For `exec`, calls run_exec
        directly with an on_start callback to capture the subprocess handle.
        """
        spec = self._registry.get(block.name)
        if spec is None:
            return ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=f"Unknown tool: {block.name}",
                is_error=True,
            )

        try:
            if block.name == "exec":
                # exec tool: run via subprocess with on_start for cancellation
                source = block.input.get("source", "")
                kwargs: dict = {"on_start": self._on_proc_start}
                if self._exec_python:
                    kwargs["python"] = self._exec_python
                if self._exec_run_root:
                    kwargs["run_root"] = self._exec_run_root
                envelope = await run_exec(source, **kwargs)
                content = format_result(envelope)
                is_error = not envelope.ok
            else:
                # Generic tool handler call
                result = await spec.handler(**block.input)
                content = str(result)
                is_error = False
        except Exception as e:
            content = f"{type(e).__name__}: {e}"
            is_error = True
        finally:
            self._active_proc = None

        return ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=content,
            is_error=is_error,
        )

    def _on_proc_start(self, proc: asyncio.subprocess.Process) -> None:
        """Callback from run_exec to capture the active subprocess handle."""
        self._active_proc = proc

    def _cancel_active_proc(self) -> None:
        """Cancel the active runner subprocess if any (SIGTERM, then KILL)."""
        proc = self._active_proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        # Schedule a kill after grace period (non-blocking)
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(2.0, self._kill_proc, proc)
        except RuntimeError:
            # No running loop (called from non-async context) — skip delayed kill
            pass

    def _kill_proc(self, proc: asyncio.subprocess.Process) -> None:
        """Kill a subprocess if it hasn't exited after grace period."""
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

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

    def _persist_tool_call(self, event: ToolCall) -> None:
        """Persist a tool_call entry (JSON content: {name, source})."""
        try:
            content = json.dumps({"name": event.name, **event.input}, ensure_ascii=False)
            entry = MessageEntry(
                id=str(ULID()),
                when=datetime.now(UTC).isoformat(),
                role="tool_call",
                content=content,
            )
            write_entry(self.log_path, entry)
        except Exception:
            log.warning("Failed to persist tool_call", exc_info=True)

    def _persist_tool_result(self, event: ToolResult) -> None:
        """Persist a tool_result entry (content = model-facing result string)."""
        try:
            entry = MessageEntry(
                id=str(ULID()),
                when=datetime.now(UTC).isoformat(),
                role="tool_result",
                content=event.content,
            )
            write_entry(self.log_path, entry)
        except Exception:
            log.warning("Failed to persist tool_result", exc_info=True)

    def _persist_assistant(
        self,
        content: str,
        usage: TurnUsage | None,
        interrupted: bool,
    ) -> None:
        """Persist an assistant message with metadata (best-effort)."""
        try:
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

    @staticmethod
    def _summarise_tool_input(input_dict: dict) -> str:
        """Return tool input for wire events (full source for TUI display)."""
        return input_dict.get("source", "")

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
