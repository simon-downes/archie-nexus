"""Agent harness — stateful orchestrator that consumes the pure run loop.

The harness owns:
- Session state (in-memory transcript)
- Turn lifecycle (_turn_active flag, interrupt)
- Tool registry and execution
- Persistence (writes per-message JSONL entries)
- Wire event translation and broadcast to connected WebSocket clients
"""

import asyncio
import inspect
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from archie_shared.canonical_events import ModelSwitch, SessionStarted, UserMessage
from archie_shared.events import (
    IterationStart as WireIterationStart,
)
from archie_shared.events import (
    StatusUpdated,
    serialize_event,
)
from archie_shared.events import (
    TextDelta as WireTextDelta,
)
from archie_shared.events import (
    ToolCall as WireToolCall,
)
from archie_shared.events import (
    ToolResult as WireToolResult,
)
from archie_shared.events import (
    TurnComplete as WireTurnComplete,
)
from archie_shared.events import (
    TurnError as WireTurnError,
)
from archie_shared.events import (
    TurnInterrupted as WireTurnInterrupted,
)
from archie_shared.events import (
    Usage as WireUsage,
)
from archie_shared.session.log import append_event
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from starlette.websockets import WebSocket
from ulid import ULID

from archie_agent.event_log import EventFactory, now_utc
from archie_agent.events import (
    IterationStart,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    Usage,
)
from archie_agent.exec.tool import create_registry, format_result, run_exec
from archie_agent.loop import RequestContext, RequestFinished, run_loop
from archie_agent.prompt import SystemPrompt, build_system_prompt_structured, read_agents_context
from archie_agent.session import DisplayEntry, Session
from archie_agent.skills import create_skill_tool, discover_skills

if TYPE_CHECKING:
    from archie_shared.models import ModelEntry

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
        model_name: str,
        log_dir: Path,
        *,
        exec_python: str | None = None,
        exec_run_root: Path | None = None,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._model_name = model_name
        self._log_dir = log_dir

        # exec runner overrides (for testing)
        self._exec_python = exec_python
        self._exec_run_root = exec_run_root

        # Connected WebSocket clients for broadcast
        self.clients: set[WebSocket] = set()

        # Turn state
        self._turn_active = False
        self._interrupt = threading.Event()

        # Skills: discover catalog and create mutable loaded list
        self._skill_catalog = discover_skills()
        self._loaded_skills: list[tuple[str, str]] = []
        # Project rules are session-constant. Loaded skill bodies remain dynamic.
        self._agents_context = read_agents_context()

        # Tool registry: exec + skill
        self._registry = create_registry()
        skill_spec = create_skill_tool(self._skill_catalog, self._loaded_skills)
        self._registry.register(skill_spec)
        self._tool_config = self._registry.to_tool_config()

        # Active runner subprocess for cancellation
        self._active_proc: asyncio.subprocess.Process | None = None

        self._request_ids: list[str] = []
        self._event_factory = EventFactory(self.log_path, self.session.model_id, self.session.model)
        if not self.log_path.exists() or not self.log_path.read_text().strip():
            append_event(
                self.log_path,
                SessionStarted(
                    id=str(ULID()),
                    schema_version=1,
                    sent_at=now_utc(),
                    model_key=self.session.model_id,
                ),
            )

    def _build_prompt(self) -> SystemPrompt:
        """Build one structured prompt snapshot for the current outer turn."""
        return build_system_prompt_structured(
            self._model_name,
            catalog=self._skill_catalog if self._skill_catalog else None,
            loaded_skills=self._loaded_skills if self._loaded_skills else None,
            agents_context=self._agents_context,
        )

    def switch_model(self, model_key: str, model: "ModelEntry", llm_client: "LLMClient") -> None:
        """Switch the active model mid-session.

        Updates the LLM client, session state, and model name used for prompt
        rebuild on the next turn.

        Args:
            model_key: Catalog key for the model.
            model: The new ModelEntry from the catalog.
            llm_client: Pre-built LLM client for the new model.
        """
        self._llm = llm_client
        self._model_name = model.name
        self.session.model_id = model_key
        self.session.model = model
        self._event_factory = EventFactory(self.log_path, model_key, model)
        append_event(
            self.log_path, ModelSwitch(id=str(ULID()), model_key=model_key, sent_at=now_utc())
        )

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
            await self._broadcast(
                WireTurnError(turn_index=turn_index, message="Turn already active")
            )
            return

        self._turn_active = True
        turn_index = self.session.next_turn_index()
        self._request_ids = []
        current_request_id = ""
        current_iteration = 0

        # Persist canonical user message before streaming starts.
        append_event(
            self.log_path,
            UserMessage(
                id=str(ULID()),
                turn=turn_index,
                scope=self._event_factory.scope,
                content=content,
            ),
        )

        # Add user message to in-memory transcript
        self.session.add_turn(role="user", content=content, turn_index=turn_index)

        # Run the pure loop and consume events
        assistant_text = ""
        last_usage: Usage | None = None

        # Per-iteration accumulators to reconstruct the tool-augmented transcript
        # so intermediate context (tool calls + results) survives into the next
        # user turn — even when the turn ends via error/max-iterations/interrupt.
        iter_text = ""
        iter_tool_uses: list[ToolUseBlock] = []
        iter_tool_results: list[ToolResultBlock] = []

        def _flush_iteration() -> None:
            """Persist the current iteration's assistant(tool_use)+user(tool_result)
            turns into the in-memory transcript. Text-only iterations are left to
            the terminal-event handlers (which record the final assistant turn)."""
            nonlocal iter_text, iter_tool_uses, iter_tool_results
            if not iter_tool_uses:
                iter_text = ""
                return
            assistant_blocks: list = []
            if iter_text:
                assistant_blocks.append(TextBlock(text=iter_text))
            assistant_blocks.extend(iter_tool_uses)
            self.session.add_turn(role="assistant", content=assistant_blocks, turn_index=turn_index)
            if iter_tool_results:
                self.session.add_turn(
                    role="user", content=list(iter_tool_results), turn_index=turn_index
                )
            iter_text = ""
            iter_tool_uses = []
            iter_tool_results = []

        try:
            gen = run_loop(
                messages=self.session.turns,
                system=self._build_prompt(),
                llm=self._llm,
                interrupt=self._interrupt,
                tool_config=self._tool_config,
                execute_tool=self._execute_tool,
                request_context_factory=lambda: RequestContext(str(ULID()), now_utc()),
            )

            async for event in gen:
                if isinstance(event, IterationStart):
                    # A new iteration means the previous one's tool calls +
                    # results are final — persist them to the transcript.
                    if event.index > 0:
                        _flush_iteration()
                    current_iteration = event.index
                    self._event_factory.iteration_start(
                        turn_iteration=f"{turn_index}.{event.index}",
                        index=event.index,
                    )
                    await self._broadcast(
                        WireIterationStart(turn_index=turn_index, index=event.index)
                    )

                elif isinstance(event, TextDelta):
                    assistant_text += event.text
                    iter_text += event.text
                    await self._broadcast(WireTextDelta(turn_index=turn_index, text=event.text))

                elif isinstance(event, RequestFinished):
                    request, serialized = self._event_factory.request(
                        turn_iteration=f"{turn_index}.{current_iteration}",
                        sent_at=event.context.sent_at,
                        duration_ms=event.duration_ms,
                        status=event.status,
                        usage=event.usage,
                        stop_reason=event.stop_reason,
                        error=event.error,
                        request_id=event.context.request_id,
                    )
                    self._request_ids.append(request.id)
                    current_request_id = request.id
                    await self._broadcast_raw(serialized)

                elif isinstance(event, Usage):
                    last_usage = event
                    self.session.record_usage(
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        cache_read_tokens=event.cache_read_tokens,
                        cache_write_tokens=event.cache_write_tokens,
                    )
                    await self._broadcast(
                        WireUsage(
                            turn_index=turn_index,
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                            cache_read_tokens=event.cache_read_tokens,
                            cache_write_tokens=event.cache_write_tokens,
                            context_pct=self.session.context_pct,
                        )
                    )

                elif isinstance(event, ToolCall):
                    # Accumulate for transcript reconstruction
                    iter_tool_uses.append(
                        ToolUseBlock(
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input=event.input,
                        )
                    )
                    # Persist canonical tool_call event.
                    self._event_factory.tool_call(
                        turn_iteration=f"{turn_index}.{current_iteration}",
                        request_id=current_request_id,
                        tool_use_id=event.tool_use_id,
                        name=event.name,
                        input=event.input,
                    )
                    # Broadcast wire event with raw input; client formats.
                    await self._broadcast(
                        WireToolCall(
                            turn_index=turn_index,
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input=event.input,
                        )
                    )

                elif isinstance(event, ToolResult):
                    # Accumulate for transcript reconstruction
                    iter_tool_results.append(
                        ToolResultBlock(
                            tool_use_id=event.tool_use_id,
                            content=event.content,
                            is_error=event.is_error,
                        )
                    )
                    # Extract duration from content (format: "duration: NNNms")
                    duration_ms = _extract_duration_ms(event.content)
                    result_bytes = len(event.content.encode("utf-8")) if event.content else 0
                    # Persist canonical tool_result event.
                    self._event_factory.tool_result(
                        turn_iteration=f"{turn_index}.{current_iteration}",
                        request_id=current_request_id,
                        tool_use_id=event.tool_use_id,
                        content=event.content,
                        is_error=event.is_error,
                        duration_ms=duration_ms,
                        result_bytes=result_bytes,
                    )
                    # Broadcast wire event with raw content; client formats.
                    await self._broadcast(
                        WireToolResult(
                            turn_index=turn_index,
                            tool_use_id=event.tool_use_id,
                            is_error=event.is_error,
                            content=event.content,
                            duration_ms=duration_ms,
                            result_bytes=result_bytes,
                        )
                    )

                elif isinstance(event, TurnComplete):
                    # Final iteration is text-only (stop_reason=end_turn); any
                    # earlier tool iterations were already flushed. Record only
                    # the trailing text to avoid duplicating flushed turns.
                    if iter_text or not self.session.turns or self.session.turns[-1].role != "user":
                        self.session.add_turn(
                            role="assistant",
                            content=iter_text,
                            turn_index=turn_index,
                            output_tokens=last_usage.output_tokens if last_usage else 0,
                        )
                    self._event_factory.assistant_message(
                        turn=turn_index,
                        request_ids=self._request_ids,
                        content=assistant_text,
                        interrupted=False,
                    )
                    self._event_factory.turn_complete(
                        turn=turn_index, stop_reason=event.stop_reason
                    )
                    await self._broadcast(
                        WireTurnComplete(turn_index=turn_index, stop_reason=event.stop_reason)
                    )

                elif isinstance(event, TurnError):
                    # Flush the final (unterminated) iteration's tool calls +
                    # results so their context survives into the next turn.
                    _flush_iteration()
                    if iter_text:
                        self.session.add_turn(
                            role="assistant",
                            content=iter_text,
                            turn_index=turn_index,
                            interrupted=True,
                        )
                    if assistant_text:
                        self._event_factory.assistant_message(
                            turn=turn_index,
                            request_ids=self._request_ids,
                            content=assistant_text,
                            interrupted=True,
                        )
                    # Persist and record the error for history replay
                    self._event_factory.turn_error(turn=turn_index, message=event.error)
                    self.session.display_entries.append(
                        DisplayEntry(role="error", content=event.error, turn_index=turn_index)
                    )
                    await self._broadcast(WireTurnError(turn_index=turn_index, message=event.error))

                elif isinstance(event, TurnInterrupted):
                    # Flush the final (unterminated) iteration. The loop already
                    # appends "cancelled" repair results on interrupt, but those
                    # live only inside run_loop; reconstruct here from events.
                    _flush_iteration()
                    if iter_text:
                        self.session.add_turn(
                            role="assistant",
                            content=iter_text,
                            turn_index=turn_index,
                            output_tokens=last_usage.output_tokens if last_usage else 0,
                            interrupted=True,
                        )
                    if assistant_text:
                        self._event_factory.assistant_message(
                            turn=turn_index,
                            request_ids=self._request_ids,
                            content=assistant_text,
                            interrupted=True,
                        )
                    # Persist and record the interruption for history replay
                    self._event_factory.turn_interrupted(turn=turn_index)
                    self.session.display_entries.append(
                        DisplayEntry(role="interrupted", content="", turn_index=turn_index)
                    )
                    await self._broadcast(WireTurnInterrupted(turn_index=turn_index))

        except Exception as e:
            log.exception("Error in harness event consumption")
            await self._broadcast(WireTurnError(turn_index=turn_index, message=str(e)))

        finally:
            self._turn_active = False
            # Broadcast status refresh (git branch may have changed during the turn)
            from archie_agent.app import _read_git_branch

            await self._broadcast(StatusUpdated(git_branch=_read_git_branch()))

    def interrupt(self) -> None:
        """Signal the current turn to stop. Also cancels any active subprocess."""
        self._interrupt.set()
        self._cancel_active_proc()

    async def _execute_tool(self, block: ToolUseBlock) -> ToolResultBlock:
        """Execute a tool_use block and return a ToolResultBlock.

        Uses the tool registry to find the handler. For `exec`, calls run_exec
        directly with an on_start callback to capture the subprocess handle.
        Generic tools that declare an `on_start` parameter (e.g. `shell`) also
        receive the callback so their subprocess can be cancelled via interrupt.
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
                # Generic tool handler call. Inject on_start for tools that
                # declare it (e.g. shell) so the harness can capture the
                # subprocess handle and cancel it on interrupt (ESC).
                kwargs = dict(block.input)
                if "on_start" in inspect.signature(spec.handler).parameters:
                    kwargs["on_start"] = self._on_proc_start
                result = await spec.handler(**kwargs)
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

    async def _broadcast_raw(self, data: str) -> None:
        """Broadcast an already-canonical serialized event."""
        disconnected = set()
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.add(ws)
        self.clients -= disconnected

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
