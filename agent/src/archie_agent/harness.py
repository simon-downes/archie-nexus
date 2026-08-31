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
import signal
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from archie_shared.canonical_events import (
    ErrorNotice,
    SessionStarted,
    StatusUpdated,
    UserMessage,
    decode_event,
)
from archie_shared.canonical_events import (
    TextDelta as CanonicalTextDelta,
)
from archie_shared.schemas import SubagentsConfig
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from ulid import ULID

from archie_agent.agents import AgentEntry, create_task_tool, discover_agents
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
from archie_agent.exec.tools._subprocess import kill_process_group
from archie_agent.loop import RequestContext, RequestFinished, run_loop
from archie_agent.prompt import SystemPrompt, build_system_prompt_structured, read_agents_context
from archie_agent.session import DisplayEntry, Session
from archie_agent.session_bus import SessionEventBus
from archie_agent.skills import create_skill_tool, discover_skills

if TYPE_CHECKING:
    from archie_shared.models import ModelEntry

    from archie_agent.llm import LLMClient

log = logging.getLogger(__name__)


def _shell_result_is_error(content: str) -> bool:
    """Return whether the shell result's first line reports failure."""
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if first_line.startswith("[error:"):
        return True
    if not first_line.startswith("[exit:"):
        return False
    try:
        return int(first_line.removeprefix("[exit:").removesuffix("]").strip()) != 0
    except ValueError:
        return False


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
        model_catalog: dict[str, "ModelEntry"] | None = None,
        region: str = "eu-west-1",
        subagents: SubagentsConfig | None = None,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._model_name = model_name
        self._log_dir = log_dir
        self._model_catalog = model_catalog or {}
        self._region = region
        self._subagents = subagents or SubagentsConfig()
        if self._subagents.max_concurrent <= 0:
            raise ValueError("agent.subagents.max_concurrent must be positive")

        # exec runner overrides (for testing)
        self._exec_python = exec_python
        self._exec_run_root = exec_run_root

        # Session-global ordered persistence and client delivery.
        self._event_bus = SessionEventBus(self.log_path)

        # Turn state
        self._turn_active = False
        self._interrupt = threading.Event()

        # Agent and skill catalogs are session-constant.
        self._agent_catalog: dict[str, AgentEntry] = discover_agents()
        self._skill_catalog = discover_skills()
        self._loaded_skills: list[tuple[str, str]] = []
        # Project rules are session-constant. Loaded skill bodies remain dynamic.
        self._agents_context = read_agents_context()

        # Tool registry: exec + skill
        self._registry = create_registry()
        skill_spec = create_skill_tool(self._skill_catalog, self._loaded_skills)
        self._registry.register(skill_spec)
        self._current_turn_index = 0
        self._children: dict[tuple[str, int], tuple[threading.Event, asyncio.Event, callable]] = {}
        self._registry.register(
            create_task_tool(
                agent_catalog=self._agent_catalog,
                skill_catalog=self._skill_catalog,
                session=self.session,
                model_catalog=self._model_catalog,
                active_model=lambda: self.session.model,
                active_model_key=lambda: self.session.model_id,
                region=self._region,
                log_path=self.log_path,
                broadcast=self._publish_serialized,
                exec_python=self._exec_python,
                exec_run_root=self._exec_run_root,
                max_concurrent=self._subagents.max_concurrent,
                live_children=self._children,
            )
        )
        self._tool_config = self._registry.to_tool_config()

        # Active runner subprocess for cancellation
        self._active_proc: asyncio.subprocess.Process | None = None

        # Event-driven interrupt bridge: the threading.Event is used by provider
        # streaming; the asyncio.Event is set via call_soon_threadsafe so the
        # tool batch can race against it without polling. Both are (re)created
        # per turn in handle_message once the running loop is known.
        self._interrupt_async: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._request_ids: list[str] = []
        self._event_factory = EventFactory(self.log_path, self.session.model_id, self.session.model)
        if not self.log_path.exists() or not self.log_path.read_text().strip():
            self._event_bus.append(
                SessionStarted(
                    id=str(ULID()),
                    schema_version=1,
                    sent_at=now_utc(),
                    model_key=self.session.model_id,
                )
            )

    def _make_task_placeholder(self):
        """Create the root task tool placeholder before task wiring is added."""
        from archie_agent.tools import ToolSpec

        async def handler(tasks=None, **kwargs):
            return "Error: task tool is not yet wired"

        return ToolSpec(
            name="task",
            description="Delegate one or more tasks to specialised child agents.",
            schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "object"},
                    },
                },
                "required": ["tasks"],
            },
            handler=handler,
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

    @property
    def log_path(self) -> Path:
        """Path to the JSONL log file for this session."""
        return self._log_dir / f"{self.session.session_id}.jsonl"

    @property
    def clients(self):
        """Set-like view of clients owned by the session event bus."""
        return self._event_bus.clients

    @property
    def event_bus(self) -> SessionEventBus:
        return self._event_bus

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    async def handle_message(self, content: str) -> None:
        """Process a user message: stream LLM response and broadcast events.

        Clears the interrupt flag at entry to prevent stale flags leaking.
        """
        self._interrupt.clear()
        # Capture the running loop and (re)create the async interrupt bound to
        # it so interrupt() can wake the tool batch via call_soon_threadsafe.
        self._loop = asyncio.get_running_loop()
        self._interrupt_async = asyncio.Event()

        if self._turn_active:
            turn_index = self.session.turn_index or 1
            await self._event_bus.broadcast(
                ErrorNotice(
                    id=str(ULID()),
                    kind="turn_active",
                    message="Turn already active",
                )
            )
            return

        self._turn_active = True
        turn_index = self.session.next_turn_index()
        self._current_turn_index = turn_index
        self._request_ids = []
        current_request_id = ""
        current_iteration = 0

        # Persist and enqueue the user message before provider streaming starts.
        await self._event_bus.publish(
            UserMessage(
                id=str(ULID()),
                turn=turn_index,
                scope=self._event_factory.scope,
                content=content,
            )
        )

        # Add user message to in-memory transcript
        self.session.add_turn(role="user", content=content, turn_index=turn_index)

        # Run the pure loop and consume events
        last_usage: Usage | None = None

        # Per-iteration accumulators to reconstruct the tool-augmented transcript
        # so intermediate context (tool calls + results) survives into the next
        # user turn — even when the turn ends via error/max-iterations/interrupt.
        iter_text = ""
        iter_tool_uses: list[ToolUseBlock] = []
        iter_tool_results: list[ToolResultBlock] = []
        assistant_event_logged = False

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
                interrupt_async=self._interrupt_async,
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
                    iter_text = ""
                    assistant_event_logged = False
                    current_iteration = event.index
                    iteration, _ = self._event_factory.iteration_start(
                        turn_iteration=f"{turn_index}.{event.index}",
                        index=event.index,
                    )
                    await self._event_bus.publish(iteration)

                elif isinstance(event, TextDelta):
                    iter_text += event.text
                    delta, _ = self._event_factory.text_delta(
                        turn_iteration=f"{turn_index}.{current_iteration}",
                        request_id=current_request_id,
                        text=event.text,
                    )
                    await self._event_bus.broadcast(delta)

                elif isinstance(event, RequestFinished):
                    request, _ = self._event_factory.request(
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
                    await self._event_bus.publish(request)

                elif isinstance(event, Usage):
                    last_usage = event
                    self.session.record_usage(
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        cache_read_tokens=event.cache_read_tokens,
                        cache_write_tokens=event.cache_write_tokens,
                    )

                elif isinstance(event, ToolCall):
                    if iter_text and not assistant_event_logged:
                        assistant, _ = self._event_factory.assistant_message(
                            turn=turn_index,
                            turn_iteration=f"{turn_index}.{current_iteration}",
                            request_ids=self._request_ids.copy(),
                            content=iter_text,
                            interrupted=False,
                        )
                        await self._event_bus.publish(assistant)
                        assistant_event_logged = True
                    # Accumulate for transcript reconstruction
                    iter_tool_uses.append(
                        ToolUseBlock(
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input=event.input,
                        )
                    )
                    tool_call, _ = self._event_factory.tool_call(
                        turn_iteration=f"{turn_index}.{current_iteration}",
                        request_id=current_request_id,
                        tool_use_id=event.tool_use_id,
                        name=event.name,
                        input=event.input,
                    )
                    await self._event_bus.publish(tool_call)

                elif isinstance(event, ToolResult):
                    # Accumulate for transcript reconstruction
                    iter_tool_results.append(
                        ToolResultBlock(
                            tool_use_id=event.tool_use_id,
                            content=event.content,
                            is_error=event.is_error,
                        )
                    )
                    duration_ms = event.duration_ms
                    result_lines = event.result_lines
                    result_bytes = event.result_bytes if event.content else 0
                    tool_result, _ = self._event_factory.tool_result(
                        turn_iteration=f"{turn_index}.{current_iteration}",
                        request_id=current_request_id,
                        tool_use_id=event.tool_use_id,
                        content=event.content,
                        is_error=event.is_error,
                        duration_ms=duration_ms,
                        result_bytes=result_bytes,
                        result_lines=result_lines,
                    )
                    await self._event_bus.publish(tool_result)

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
                    if iter_text and not assistant_event_logged:
                        assistant, _ = self._event_factory.assistant_message(
                            turn=turn_index,
                            turn_iteration=f"{turn_index}.{current_iteration}",
                            request_ids=self._request_ids.copy(),
                            content=iter_text,
                            interrupted=False,
                        )
                        await self._event_bus.publish(assistant)
                    complete, _ = self._event_factory.turn_complete(
                        turn=turn_index, stop_reason=event.stop_reason
                    )
                    await self._event_bus.publish(complete)

                elif isinstance(event, TurnError):
                    # Persist any final assistant text before flushing the
                    # iteration's tool context.
                    if iter_text and not assistant_event_logged:
                        assistant, _ = self._event_factory.assistant_message(
                            turn=turn_index,
                            turn_iteration=f"{turn_index}.{current_iteration}",
                            request_ids=self._request_ids.copy(),
                            content=iter_text,
                            interrupted=True,
                        )
                        await self._event_bus.publish(assistant)
                    _flush_iteration()
                    if iter_text:
                        self.session.add_turn(
                            role="assistant",
                            content=iter_text,
                            turn_index=turn_index,
                            interrupted=True,
                        )
                    error_event, _ = self._event_factory.turn_error(
                        turn=turn_index, message=event.error
                    )
                    await self._event_bus.publish(error_event)
                    self.session.display_entries.append(
                        DisplayEntry(role="error", content=event.error, turn_index=turn_index)
                    )

                elif isinstance(event, TurnInterrupted):
                    # Persist any final assistant text before flushing the
                    # iteration's tool context.
                    if iter_text and not assistant_event_logged:
                        assistant, _ = self._event_factory.assistant_message(
                            turn=turn_index,
                            turn_iteration=f"{turn_index}.{current_iteration}",
                            request_ids=self._request_ids.copy(),
                            content=iter_text,
                            interrupted=True,
                        )
                        await self._event_bus.publish(assistant)
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
                    interrupted, _ = self._event_factory.turn_interrupted(turn=turn_index)
                    await self._event_bus.publish(interrupted)
                    self.session.display_entries.append(
                        DisplayEntry(role="interrupted", content="", turn_index=turn_index)
                    )

        except Exception as e:
            log.exception("Error in harness event consumption")
            await self._event_bus.broadcast(
                ErrorNotice(id=str(ULID()), kind="turn_error", message=str(e))
            )

        finally:
            self._turn_active = False
            # Broadcast status refresh (git branch may have changed during the turn).
            from archie_agent.app import _read_git_branch

            await self._event_bus.broadcast(
                StatusUpdated(id=str(ULID()), git_branch=_read_git_branch())
            )

    def interrupt(self, target: tuple[str, int] | None = None) -> None:
        """Signal the current turn or one child to stop."""
        if target is not None:
            child = self._children.get(target)
            if child is None:
                log.debug("Ignoring stale child interrupt target %s", target)
                return
            interrupt, async_interrupt, cancel_process = child
            interrupt.set()
            async_interrupt.set()
            cancel_process()
            return

        for interrupt, async_interrupt, cancel_process in list(self._children.values()):
            interrupt.set()
            async_interrupt.set()
            cancel_process()

        self._interrupt.set()
        loop = self._loop
        async_interrupt = self._interrupt_async
        if loop is not None and async_interrupt is not None:
            loop.call_soon_threadsafe(async_interrupt.set)
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

        started = time.perf_counter()
        try:
            if block.name == "task":
                kwargs = dict(block.input)
                kwargs["_launch_scope"] = block.tool_use_id
                kwargs["_parent_turn"] = self._current_turn_index
                result = await spec.handler(**kwargs)
                content = str(result)
                is_error = content.startswith("Error:")
            elif block.name == "exec":
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
                is_error = block.name == "shell" and _shell_result_is_error(content)
        except Exception as e:
            content = f"{type(e).__name__}: {e}"
            is_error = True
        finally:
            self._active_proc = None

        duration_ms = round((time.perf_counter() - started) * 1000)
        return ToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=content,
            is_error=is_error,
            duration_ms=duration_ms,
            result_lines=len(content.splitlines()),
            result_bytes=len(content.encode("utf-8")),
        )

    def _on_proc_start(self, proc: asyncio.subprocess.Process) -> None:
        """Capture the active tool subprocess handle for cancellation.

        Invoked by run_exec and by generic tools (e.g. shell) that spawn a
        subprocess, so interrupt() can kill it (and its whole process group).
        """
        self._active_proc = proc

    def _cancel_active_proc(self) -> None:
        """Terminate the active tool subprocess GROUP if any (SIGTERM, then KILL).

        Tool subprocesses are spawned with start_new_session=True, so the child
        and all its descendants (e.g. sh -> uv -> python -> pytest) share one
        process group. Signalling only the direct child would leave the real
        workload running and keep the turn wedged; we signal the whole group.
        """
        proc = self._active_proc
        if proc is None or proc.returncode is not None:
            return
        kill_process_group(proc, signal.SIGTERM)
        # Schedule a group KILL after a grace period (non-blocking).
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(2.0, self._kill_proc, proc)
        except RuntimeError:
            # No running loop (called from non-async context) — skip delayed kill
            pass

    def _kill_proc(self, proc: asyncio.subprocess.Process) -> None:
        """SIGKILL the subprocess group if it hasn't exited after the grace period."""
        if proc.returncode is None:
            kill_process_group(proc, signal.SIGKILL)

    async def _publish_serialized(self, data: str) -> None:
        """Decode a child factory result and route it through the bus."""
        event = decode_event(data)
        if isinstance(event, CanonicalTextDelta):
            await self._event_bus.broadcast(event)
        else:
            await self._event_bus.publish(event)
