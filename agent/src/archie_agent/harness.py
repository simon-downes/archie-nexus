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

import msgspec
from archie_shared.events import (
    CompactionComplete,
    CompactionStart,
    ErrorNotice,
    Event,
    SessionEvent,
    SessionStatus,
    UserMessage,
)
from archie_shared.events import TurnError as CanonicalTurnError
from archie_shared.models import DEFAULT_MODELS, calculate_cost
from archie_shared.schemas import CompactionConfig, SubagentsConfig
from archie_shared.session.log import LogAppendError, SessionLog
from archie_shared.types import ImageBlock, TextBlock, ToolResultBlock, ToolUseBlock
from ulid import ULID

from archie_agent.agents import AgentEntry, create_task_tool, discover_agents
from archie_agent.compaction import Compactor, build_replacement, effective_limit, estimate_tokens
from archie_agent.event_factory import EventFactory, now_utc
from archie_agent.exec.tool import create_registry, format_result, run_exec
from archie_agent.exec.tools._subprocess import kill_process_group
from archie_agent.exec.tools.images import ToolImage
from archie_agent.llm import create_llm_client
from archie_agent.loop import PreRequest, run_loop
from archie_agent.loop_events import (
    IterationStart,
    RequestContext,
    RequestFinished,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    Usage,
)
from archie_agent.prompt import SystemPrompt, build_system_prompt_structured, read_agents_context
from archie_agent.session import DisplayEntry, Session, Turn
from archie_agent.session_bus import SessionEventBus
from archie_agent.skills import create_skill_tool, discover_skills
from archie_agent.tool_policy_live import LiveToolPolicy

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
        tool_policy: "dict | LiveToolPolicy | None" = None,
        turn_cost_limit: float = 50.0,
        compaction: CompactionConfig | None = None,
        compaction_llm: "LLMClient | None" = None,
        clock=time.monotonic,
    ) -> None:
        self.session = session
        self._llm = llm_client
        self._model_name = model_name
        self._log_dir = log_dir
        self._model_catalog = model_catalog or {}
        self._region = region
        self._subagents = subagents or SubagentsConfig()
        self._tool_policy = (
            tool_policy
            if isinstance(tool_policy, LiveToolPolicy)
            else LiveToolPolicy.static(tool_policy)
        )
        self._turn_cost_limit = turn_cost_limit
        self._baseline_context_tokens: int | None = None
        self._baseline_turn_count = 0
        self._compaction_failed_this_turn = False
        self._clock = clock
        self._last_request_finished: float | None = None
        self._cache_cold = False
        self._compaction_config = compaction or CompactionConfig()
        self._compaction_model_key = self._compaction_config.model
        # Startup validates the key (app.lifespan); never substitute the session
        # model, which would mislabel compaction requests and their cost.
        compaction_catalog = model_catalog if model_catalog is not None else DEFAULT_MODELS
        if self._compaction_model_key not in compaction_catalog:
            raise ValueError(f"Unknown compaction model: {self._compaction_model_key}")
        self._compaction_model = compaction_catalog[self._compaction_model_key]
        if compaction_llm is None:
            self._compaction_llm = create_llm_client(
                msgspec.structs.replace(
                    self._compaction_model,
                    can_cache=False,
                    max_output_tokens=min(16_384, self._compaction_model.max_output_tokens),
                ),
                self._region,
            )
        else:
            self._compaction_llm = compaction_llm
        if self._turn_cost_limit <= 0:
            raise ValueError("agent.turn_cost_limit must be positive")
        if self._subagents.max_concurrent <= 0:
            raise ValueError("agent.subagents.max_concurrent must be positive")

        # exec runner overrides (for testing)
        self._exec_python = exec_python
        self._exec_run_root = exec_run_root

        # Session-global ordered persistence and client delivery.
        self._event_bus = SessionEventBus(SessionLog(self.log_path))

        # Turn state
        self._turn_active = False
        self._interrupt = threading.Event()

        # Agent and skill catalogs are session-constant.
        self._agent_catalog: dict[str, AgentEntry] = discover_agents()
        self._skill_catalog = discover_skills(self._tool_policy.enabled)
        self._loaded_content_keys: set[tuple[str, str]] = set()
        # Project rules are session-constant. Loaded skill bodies remain dynamic.
        self._agents_context = read_agents_context()

        # Tool registry: exec + skill
        self._registry = create_registry()
        skill_spec = create_skill_tool(self._skill_catalog, self._loaded_content_keys)
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
                emit=self._emit_event,
                exec_python=self._exec_python,
                exec_run_root=self._exec_run_root,
                runner_context=self._current_runner_context,
                turn_cost_limit=self._turn_cost_limit,
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

        self._event_factory = EventFactory(self.log_path, self.session.model_id, self.session.model)

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
        self._baseline_context_tokens = None
        self._baseline_turn_count = 0
        self._cache_cold = True

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

    def try_begin_turn(self) -> bool:
        """Atomically admit one root turn on the event-loop thread."""
        if self._turn_active:
            return False
        self._turn_active = True
        return True

    async def handle_message(self, content: str) -> None:
        """Run one accepted turn and guarantee terminal cleanup."""
        if not self._turn_active:
            # Direct callers (tests and local integrations) have no stream
            # admission step; the WebSocket path calls try_begin_turn first.
            self._turn_active = True
        self._terminal_emitted = False
        self._storage_failed = False
        self._fallback_attempted = False
        try:
            await self._handle_message_body(content)
        except LogAppendError as exc:
            self._storage_failed = True
            await self._broadcast_storage_error(str(exc))
        except Exception as exc:  # noqa: BLE001 — guarantee a terminal event
            log.exception("Error in harness event consumption")
            if not self._terminal_emitted and not self._fallback_attempted:
                self._fallback_attempted = True
                await self._publish_fallback_terminal(str(exc))
        finally:
            if (
                not self._terminal_emitted
                and not self._storage_failed
                and not self._fallback_attempted
            ):
                self._fallback_attempted = True
                await self._publish_fallback_terminal("Turn ended without a terminal event")
            self._turn_active = False
            from archie_agent.app import _read_git_branch

            try:
                await self._event_bus.emit(
                    SessionStatus(
                        id=str(ULID()),
                        model_key=self.session.model_id,
                        git_branch=_read_git_branch(),
                    )
                )
            except Exception:  # noqa: BLE001 — status refresh is best effort
                log.warning("Failed to broadcast final turn status", exc_info=True)

    def _output_reserve(self) -> int:
        return min(self.session.model.max_output_tokens, 16_384)

    def _estimate_context(self, turns: list, *, use_baseline: bool = True) -> int:
        """Estimated input context tokens for ``turns``, excluding the output reserve."""
        if use_baseline and self._baseline_context_tokens is not None:
            new_turns = turns[self._baseline_turn_count :]
            return self._baseline_context_tokens + estimate_tokens(None, None, new_turns)
        return estimate_tokens(self._build_prompt(), self._tool_config, turns)

    def _over_limit(self, context_tokens: int) -> bool:
        limit = effective_limit(self.session.model, self._compaction_config)
        return context_tokens + self._output_reserve() >= limit

    async def _before_request(self, messages: list, iteration: int):
        if self._compaction_failed_this_turn:
            return None
        estimate = self._estimate_context(messages)
        threshold = self._over_limit(estimate)
        cold = False
        estimated_next_request_cost_usd = None
        ttl = self.session.model.cache_ttl_seconds
        if iteration == 0 and ttl is not None:
            cache_expired = self._cache_cold or (
                self._last_request_finished is not None
                and self._clock() - self._last_request_finished > ttl
            )
            rate = self.session.model.cost.get_rate(estimate, 0)
            price = rate.cache_write or rate.input
            next_request_cost = estimate * price / 1_000_000
            cold = cache_expired and next_request_cost > self._compaction_config.cold_cost_threshold
            if cold:
                estimated_next_request_cost_usd = next_request_cost
        reason = "cold_cache" if cold else "threshold" if threshold else None
        if reason is None:
            return None
        source_messages = messages
        pending = messages[-1].content if iteration == 0 else []
        if iteration == 0 and messages and messages[-1].compacted:
            # The new user message is the final block merged into the compacted
            # turn; earlier blocks (framing, summary, any unanswered message from
            # a failed turn) are history and get summarised.
            compacted_turn = messages[-1]
            source_messages = [Turn("user", compacted_turn.content[:-1], compacted=True)]
            pending = compacted_turn.content[-1:]
        elif iteration == 0:
            source_messages = messages[:-1]
        compacted = await self._compact(
            source_messages,
            reason=reason,
            mode="start_of_turn" if iteration == 0 else "mid_turn",
            pending=pending,
            turn=self._current_turn_index,
            iteration=iteration,
            tokens_before=estimate,
            estimated_next_request_cost_usd=estimated_next_request_cost_usd,
        )
        if compacted is None:
            self._compaction_failed_this_turn = True
            return None
        replacement, tokens_after = compacted
        if self._over_limit(tokens_after):
            return PreRequest(error="context still exceeds compaction limit after compaction")
        return PreRequest(messages=[replacement])

    async def _compact(
        self,
        source: list[Turn],
        *,
        reason: str,
        mode: str,
        pending: list,
        turn: int,
        iteration: int,
        tokens_before: int,
        estimated_next_request_cost_usd: float | None,
    ) -> tuple[Turn, int] | None:
        """Summarise ``source`` and replace the root transcript.

        Shared by manual and automatic compaction. Emits ``compaction_start``, the
        compaction ``llm_request`` when the provider was reached, and one
        ``compaction_complete`` carrying the success, failure or interruption.
        Returns the replacement turn and its estimated context tokens (excluding
        the output reserve), or ``None`` on failure, leaving the transcript
        unchanged.
        """
        request_id = str(ULID())
        started_at = now_utc()
        await self._event_bus.emit(
            CompactionStart(
                id=str(ULID()),
                turn=turn,
                iteration=iteration,
                scope=None,
                request_id=request_id,
                reason=reason,
                tokens_before=tokens_before,
                estimated_next_request_cost_usd=estimated_next_request_cost_usd,
                model_key=self._compaction_model_key,
                sent_at=started_at,
            )
        )
        outcome = await Compactor(
            self._compaction_model_key, self._compaction_model, self._compaction_llm
        ).run(
            source,
            reason=reason,
            turn=turn,
            iteration=iteration,
            interrupt=self._interrupt,
            request_id=request_id,
        )
        if outcome.request_event is not None:
            await self._event_bus.emit(outcome.request_event)
            self.session.record_usage(
                outcome.request_event.input_tokens,
                outcome.request_event.output_tokens,
                outcome.request_event.cache_read_tokens,
                outcome.request_event.cache_write_tokens,
                model=self._compaction_model,
                update_context=False,
            )
        if not outcome.ok:
            error = outcome.error or "Compaction failed"
            await self._event_bus.emit(
                CompactionComplete(
                    id=str(ULID()),
                    turn=turn,
                    iteration=iteration,
                    scope=None,
                    request_id=request_id,
                    reason=reason,
                    status="interrupted" if outcome.interrupted else "failure",
                    tokens_before=tokens_before,
                    tokens_after=None,
                    cost_saved_usd=0.0,
                    duration_ms=outcome.duration_ms,
                    error=error,
                    summary=None,
                    debug="",
                    estimated_next_request_cost_usd=estimated_next_request_cost_usd,
                )
            )
            return None
        replacement = build_replacement(outcome.summary, mode, pending, self.log_path)
        tokens_after = self._estimate_context([replacement], use_baseline=False)
        stats = outcome.stats
        debug = ";".join(
            [
                f"estimate_source={'baseline' if self._baseline_context_tokens is not None else 'heuristic'}",
                f"turns_in={len(source)}",
                f"tool_results_kept={stats.tool_results_kept}",
                f"tool_results_removed={stats.tool_results_removed}",
                f"orphan_results={stats.orphan_results}",
                f"rounds_dropped={stats.rounds_dropped}",
            ]
        )
        saved_rate = self.session.model.cost.get_rate(tokens_before, 0).input
        cost_saved_usd = max(0.0, (tokens_before - tokens_after) * saved_rate / 1_000_000)
        await self._event_bus.emit(
            CompactionComplete(
                id=str(ULID()),
                turn=turn,
                iteration=iteration,
                scope=None,
                request_id=request_id,
                reason=reason,
                status="success",
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                cost_saved_usd=cost_saved_usd,
                duration_ms=outcome.duration_ms,
                error=None,
                summary=outcome.summary,
                debug=debug,
                estimated_next_request_cost_usd=estimated_next_request_cost_usd,
            )
        )
        self.session.turns = [replacement]
        # Compaction removes the tool results that carried skill content. The
        # summary is not a substitute for the tagged skill/reference blocks.
        self._loaded_content_keys.clear()
        self._baseline_context_tokens = None
        self._baseline_turn_count = 0
        # The replacement prefix has never been cached, so the cold-cache check
        # has nothing to compare against until the next conversation request.
        self._cache_cold = False
        self._last_request_finished = None
        return replacement, tokens_after

    async def run_manual_compaction(self) -> None:
        """Compact the root transcript without creating a user turn."""
        self._interrupt.clear()
        self._loop = asyncio.get_running_loop()
        self._interrupt_async = asyncio.Event()
        try:
            if not self.session.turns:
                await self._event_bus.emit(
                    ErrorNotice(
                        id=str(ULID()),
                        kind="manual_compaction_failed",
                        message="Nothing to compact",
                    )
                )
                return
            await self._compact(
                self.session.turns,
                reason="manual",
                mode="manual",
                pending=[],
                turn=self.session.turn_index,
                iteration=0,
                tokens_before=self._estimate_context(self.session.turns),
                estimated_next_request_cost_usd=None,
            )
        except LogAppendError as exc:
            await self._broadcast_storage_error(str(exc))
        except Exception as exc:  # noqa: BLE001 — manual failures return to idle
            await self._event_bus.emit(
                ErrorNotice(id=str(ULID()), kind="manual_compaction_failed", message=str(exc))
            )
        finally:
            self._turn_active = False
            try:
                from archie_agent.app import _read_git_branch

                await self._event_bus.emit(
                    SessionStatus(
                        id=str(ULID()),
                        model_key=self.session.model_id,
                        git_branch=_read_git_branch(),
                    )
                )
            except Exception:  # noqa: BLE001 — status refresh is best effort
                log.warning("Failed to broadcast final compaction status", exc_info=True)

    async def _broadcast_storage_error(self, message: str) -> None:
        """Best-effort live notice when durable append fails."""
        try:
            await self._event_bus.emit(
                ErrorNotice(id=str(ULID()), kind="storage_error", message=message)
            )
        except Exception:  # noqa: BLE001 — storage error handling must not hang
            log.warning("Failed to broadcast storage error", exc_info=True)

    async def _publish_root_terminal(self, event: Event) -> None:
        """Publish a terminal event without allowing cancellation to duplicate it."""
        self._terminal_emitted = True
        try:
            await asyncio.shield(self._event_bus.emit(event))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._terminal_emitted = False
            raise

    async def _publish_fallback_terminal(self, message: str) -> None:
        """Persist one root terminal error after an unexpected turn failure."""
        try:
            try:
                terminal = self._event_factory.turn_error(
                    turn=self._current_turn_index or self.session.turn_index or 1,
                    message=message,
                )
            except Exception:
                terminal = CanonicalTurnError(
                    id=str(ULID()),
                    turn=self._current_turn_index or self.session.turn_index or 1,
                    scope=None,
                    message=message,
                )
            await self._event_bus.emit(terminal)
            self._terminal_emitted = True
        except LogAppendError as exc:
            self._storage_failed = True
            await self._broadcast_storage_error(str(exc))
        except Exception:
            log.warning("Failed to publish fallback terminal event", exc_info=True)

    async def _handle_message_body(self, content: str) -> None:
        """Process a message after stream admission has accepted its turn."""
        self._interrupt.clear()
        self._compaction_failed_this_turn = False
        # Capture the running loop and (re)create the async interrupt bound to
        # it so interrupt() can wake the tool batch via call_soon_threadsafe.
        self._loop = asyncio.get_running_loop()
        self._interrupt_async = asyncio.Event()

        turn_index = self.session.next_turn_index()
        self._current_turn_index = turn_index
        current_request_id = ""
        current_iteration = 0

        # Persist and enqueue the user message before provider streaming starts.
        await self._event_bus.emit(
            UserMessage(
                id=str(ULID()),
                turn=turn_index,
                scope=self._event_factory.scope,
                content=content,
            )
        )

        # Add user message to in-memory transcript
        merged = self.session.add_turn(role="user", content=content, turn_index=turn_index)
        if merged.compacted:
            # The message joined an existing compacted turn, which the baseline
            # already counts, so turn-count slicing would miss the new text.
            self._baseline_context_tokens = None
            self._baseline_turn_count = 0

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
                max_iterations=None,
                turn_cost_limit=self._turn_cost_limit,
                request_cost_factory=lambda usage: calculate_cost(
                    self.session.model.cost,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                ),
                request_context_factory=lambda: RequestContext(str(ULID()), now_utc()),
                before_request=self._before_request,
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
                    iteration = self._event_factory.iteration_start(
                        turn=turn_index,
                        iteration=event.index,
                    )
                    await self._event_bus.emit(iteration)

                elif isinstance(event, TextDelta):
                    iter_text += event.text
                    delta = self._event_factory.text_delta(
                        turn=turn_index,
                        iteration=current_iteration,
                        request_id=event.request_id or current_request_id,
                        text=event.text,
                    )
                    await self._event_bus.emit(delta)

                elif isinstance(event, RequestFinished):
                    request = self._event_factory.request(
                        turn=turn_index,
                        iteration=current_iteration,
                        sent_at=event.context.sent_at,
                        duration_ms=event.duration_ms,
                        status=event.status,
                        usage=event.usage,
                        stop_reason=event.stop_reason,
                        error=event.error,
                        request_id=event.context.request_id,
                    )
                    current_request_id = request.id
                    await self._event_bus.emit(request)
                    if request.purpose == "conversation" and event.usage is not None:
                        # Only a request the provider accounted for describes the
                        # real context size and refreshes the prompt cache.
                        self._baseline_context_tokens = request.context_tokens
                        self._baseline_turn_count = len(self.session.turns)
                        self._last_request_finished = self._clock()
                        self._cache_cold = False

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
                        assistant = self._event_factory.assistant_message(
                            turn=turn_index,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            content=iter_text,
                            interrupted=False,
                        )
                        await self._event_bus.emit(assistant)
                        assistant_event_logged = True
                    # Accumulate for transcript reconstruction
                    iter_tool_uses.append(
                        ToolUseBlock(
                            tool_use_id=event.tool_use_id,
                            name=event.name,
                            input=event.input,
                        )
                    )
                    tool_call = self._event_factory.tool_call(
                        turn=turn_index,
                        iteration=current_iteration,
                        request_id=current_request_id,
                        tool_use_id=event.tool_use_id,
                        name=event.name,
                        input=event.input,
                    )
                    await self._event_bus.emit(tool_call)

                elif isinstance(event, ToolResult):
                    # Accumulate for transcript reconstruction
                    iter_tool_results.append(
                        ToolResultBlock(
                            tool_use_id=event.tool_use_id,
                            content=event.content,
                            is_error=event.is_error,
                            images=event.images,
                        )
                    )
                    duration_ms = event.duration_ms
                    result_lines = event.result_lines
                    result_bytes = event.result_bytes if event.content else 0
                    tool_result = self._event_factory.tool_result(
                        turn=turn_index,
                        iteration=current_iteration,
                        request_id=current_request_id,
                        tool_use_id=event.tool_use_id,
                        content=event.content,
                        is_error=event.is_error,
                        duration_ms=duration_ms,
                        result_bytes=result_bytes,
                        result_lines=result_lines,
                        images=event.images,
                    )
                    await self._event_bus.emit(tool_result)

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
                        assistant = self._event_factory.assistant_message(
                            turn=turn_index,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            content=iter_text,
                            interrupted=False,
                        )
                        await self._event_bus.emit(assistant)
                    complete = self._event_factory.turn_complete(
                        turn=turn_index, stop_reason=event.stop_reason
                    )
                    await self._publish_root_terminal(complete)

                elif isinstance(event, TurnError):
                    # Persist any final assistant text before flushing the
                    # iteration's tool context.
                    if iter_text and not assistant_event_logged:
                        assistant = self._event_factory.assistant_message(
                            turn=turn_index,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            content=iter_text,
                            interrupted=True,
                        )
                        await self._event_bus.emit(assistant)
                    _flush_iteration()
                    if iter_text:
                        self.session.add_turn(
                            role="assistant",
                            content=iter_text,
                            turn_index=turn_index,
                            interrupted=True,
                        )
                    error_event = self._event_factory.turn_error(
                        turn=turn_index, message=event.error
                    )
                    await self._publish_root_terminal(error_event)
                    self.session.display_entries.append(
                        DisplayEntry(role="error", content=event.error, turn_index=turn_index)
                    )

                elif isinstance(event, TurnInterrupted):
                    # Persist any final assistant text before flushing the
                    # iteration's tool context.
                    if iter_text and not assistant_event_logged:
                        assistant = self._event_factory.assistant_message(
                            turn=turn_index,
                            iteration=current_iteration,
                            request_id=current_request_id,
                            content=iter_text,
                            interrupted=True,
                        )
                        await self._event_bus.emit(assistant)
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
                    interrupted = self._event_factory.turn_interrupted(turn=turn_index)
                    await self._publish_root_terminal(interrupted)
                    self.session.display_entries.append(
                        DisplayEntry(role="interrupted", content="", turn_index=turn_index)
                    )

        except Exception:
            raise

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
        images: tuple[ImageBlock, ...] = ()
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
                kwargs["runner_context"] = await self._current_runner_context()
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
                if isinstance(result, ToolImage):
                    images = (result.image,)
                    content = result.caption
                else:
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
            images=images,
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

    async def _current_runner_context(self) -> dict:
        """Runner context for one exec call: live-reloaded policy plus startup settings.

        Shows a live notice when the policy reload changes state.
        """
        snapshot, notice = self._tool_policy.refresh()
        if notice is not None:
            try:
                await self._event_bus.emit(notice)
            except Exception:  # noqa: BLE001 — a notice must never fail the exec call
                pass
        return self._tool_policy.runner_context(snapshot)

    async def _emit_event(self, event: SessionEvent) -> None:
        """Submit a typed child event through the session event bus."""
        await self._event_bus.emit(event)
