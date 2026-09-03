"""Main Textual application for the attach TUI.

Connects to a running agent session via WebSocket and displays the
conversation with streaming updates. This is a read-write client —
it can send messages and interrupts to the agent.

Threading model:
- Textual runs an asyncio event loop on the main thread
- The WS receive loop runs as an asyncio task within the same loop
- Events are dispatched to widget updates directly (no thread marshalling needed)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

import httpx
from archie_shared import events as ce
from archie_shared.commands import InterruptCommand, SwitchModelCommand
from archie_shared.events import (
    IterationStart,
    LLMRequest,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    decode_event,
)
from archie_shared.models import load_models
from archie_shared.protocol import PROTOCOL_VERSION
from archie_shared.tool_summaries import (
    format_tool_activity,
    format_tool_complete,
    format_tool_pending,
)
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from archie_cli.tui import theme
from archie_cli.tui.conversation import Conversation, IterationBlock, StreamingMessage
from archie_cli.tui.input import MessageInput
from archie_cli.tui.models_provider import ModelProvider, SubagentProvider
from archie_cli.tui.status import StatusBar
from archie_cli.tui.subagents import ChildActivityState, SubagentActivity, SubagentScreen
from archie_cli.tui.throbber import Throbber, ThrobberContainer
from archie_cli.ws_client import WSClient

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestKey:
    """Exact identity for one root or child assistant request."""

    scope: str | None
    subagent_index: int | None
    turn: int
    iteration: int
    request_id: str


@dataclass
class ChildActivity(ChildActivityState):
    """Compatibility alias for the shared child view state."""


class ArchieApp(App):
    """TUI client that attaches to a running agent session via WebSocket."""

    TITLE = "Archie"
    CSS_PATH = "archie.tcss"
    COMMANDS = App.COMMANDS | {ModelProvider, SubagentProvider}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "copy_block", "Copy Block"),
        Binding("ctrl+g", "editor", "Editor", show=False),
        Binding("ctrl+s", "subagent_picker", "Subagents"),
        Binding("ctrl+x", "stop_child", "Stop selected child"),
    ]

    def __init__(self, ws_url: str, api_url: str, container_name: str) -> None:
        super().__init__()

        # Register custom theme
        self.register_theme(theme.THEME)
        self.theme = "archie"

        self._ws_url = ws_url
        self._api_url = api_url
        self._container_name = container_name
        self._ws = WSClient()
        self._receive_task: asyncio.Task | None = None
        self._reconnecting: bool = False
        self._shutting_down: bool = False
        # Warn-once guard for protocol-version mismatch (avoids refire on reconnect)
        self._protocol_warned: bool = False
        # Canonical replay cursor: id of the latest event already rendered.
        # Used for incremental replay via /events?after= on connect/reconnect.
        self._last_event_id: str | None = None
        # Ids of canonical events already rendered — dedup across replay + live.
        self._seen_event_ids: set[str] = set()
        # Request-scoped assistant reconciliation. Durable assistant events mark
        # a key finalized so late buffered deltas for that exact request are ignored.
        self._transient_assistant_text: dict[RequestKey, str] = {}
        self._finalized_assistant_requests: set[RequestKey] = set()
        self._stream_request_key: RequestKey | None = None
        # Ids of llm_request ledger events already folded into accounting —
        # prevents double-counting across replay + live broadcast.
        self._seen_accounted_ids: set[str] = set()

        # UI state
        self._streaming: StreamingMessage | None = None
        self._stream_text: str = ""
        self._turn_active: bool = False
        self._throbber: Throbber | None = None
        self._iteration_block: IterationBlock | None = None
        self._last_esc_time: float = 0.0
        # tool_use_id -> (name, input) for client-side result formatting
        self._pending_tool_inputs: dict[str, tuple[str, dict]] = {}
        self._child_activity: dict[tuple[str, int], ChildActivity] = {}
        self._child_pending_tools: dict[tuple[str, int, str], tuple[str, dict]] = {}
        self._parent_task_inputs: dict[str, list[dict]] = {}
        self._parent_task_entries: dict[str, object] = {}
        self._child_widgets: dict[tuple[str, int], SubagentActivity] = {}
        self._active_child_key: tuple[str, int] | None = None

        # Session state for local accumulation. Seeded from Handshake.accounting
        # on connect, then updated live from broadcast llm_request ledger events.
        self._session_id: str = ""
        self._cumulative_input: int = 0
        self._cumulative_output: int = 0
        self._cumulative_cache_read: int = 0
        self._cumulative_cache_write: int = 0
        self._latest_context_tokens: int = 0
        self._latest_context_pct: float = 0.0
        self._cumulative_cost: float = 0.0
        self._turn_started_at: float | None = None
        self._turn_input = 0
        self._turn_cache_read = 0
        self._turn_cache_write = 0
        self._turn_output = 0
        self._turn_cost = 0.0
        self._turn_duration_s = 0.0

        # Live output estimation (chars/4, reconciled on LLMRequest)
        self._estimated_output: int = 0

        # Direct shell (! prefix) state
        self._shell_active: bool = False
        self._shell_proc: asyncio.subprocess.Process | None = None
        self._shell_command: str = ""
        self._shell_cancel_requested: bool = False

    def compose(self) -> ComposeResult:
        """Build the main UI layout."""
        yield Conversation(id="conversation")
        yield ThrobberContainer(id="throbber-container")
        yield StatusBar(id="status")
        yield MessageInput(id="input")
        yield Footer()

    async def on_mount(self) -> None:
        """Connect to agent and start receiving events.

        Ordering: subscribe WS first → start receive loop (buffering) → the
        Handshake frame provides the replay cursor → replay canonical
        events via /events → reconcile buffered live events by event id.
        """
        self.query_one("#input", MessageInput).focus()

        try:
            await self._ws.connect(self._ws_url)
        except Exception as e:
            self._show_client_error(f"Connection failed: {e}")
            return

        # Start receive loop FIRST — events buffer in _event_buffer
        self._event_buffer: list = []
        self._buffering = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        # Replay persisted canonical events while the receive loop buffers.
        if not await self._replay_events():
            self._buffering = False
            self._event_buffer = []
            self._show_client_error("Initial event replay failed; retrying connection.")
            if self._receive_task is not None and not self._receive_task.done():
                self._receive_task.cancel()
                try:
                    await self._receive_task
                except asyncio.CancelledError:
                    pass
            await self._ws.disconnect()
            self._schedule_reconnect()
            return

        # Reconcile: dispatch buffered live events not already rendered.
        self._buffering = False
        self._dispatch_buffered_events()

        self._event_buffer = []

    async def _replay_events(self) -> bool:
        """Fetch and render persisted canonical events, advancing the cursor.

        Requests /events?after=<cursor> when a cursor is known (reconnect),
        otherwise the full log. Each NDJSON line is a canonical event which is
        rendered via the shared reducer and deduplicated by event id.

        Returns ``False`` when the history request cannot be completed. Callers
        must not flush buffered live events or report a successful reconnect in
        that case, because those events cannot fill the history gap.
        """
        after = self._last_event_id
        url = f"{self._api_url}/events"
        if after:
            url += f"?after={after}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=5.0)
                if resp.status_code == 409:
                    # Cursor no longer exists in the log (rotated/archived). Reset
                    # every replay-derived view before rebuilding from the full log.
                    await self._reset_replay_state()
                    resp = await client.get(f"{self._api_url}/events", timeout=5.0)
                if resp.status_code != 200:
                    return False
                body = resp.text
        except Exception as e:
            log.warning("Failed to replay events: %s", e)
            return False

        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = decode_event(line, persisted=True)
            except Exception as e:  # noqa: BLE001 — skip malformed replay lines
                log.warning("Skipping malformed replay event: %s", e)
                continue
            self._render_canonical(event, replay=True)

        return True

    async def _reset_replay_state(self) -> None:
        """Clear all client state reconstructed from the persisted event stream."""
        self._last_event_id = None
        self._seen_event_ids.clear()
        self._transient_assistant_text.clear()
        self._finalized_assistant_requests.clear()
        self._stream_request_key = None
        self._seen_accounted_ids.clear()
        self._cumulative_input = 0
        self._cumulative_output = 0
        self._cumulative_cache_read = 0
        self._cumulative_cache_write = 0
        self._latest_context_tokens = 0
        self._latest_context_pct = 0.0
        self._cumulative_cost = 0.0
        self._estimated_output = 0
        self._reset_turn_metrics()
        self._turn_active = False
        self._streaming = None
        self._stream_text = ""
        self._iteration_block = None
        self._pending_tool_inputs.clear()
        self._child_activity.clear()
        self._child_pending_tools.clear()
        self._parent_task_inputs.clear()
        self._parent_task_entries.clear()
        self._child_widgets.clear()
        try:
            screen = self.screen
        except Exception:  # noqa: BLE001 — headless tests have no screen stack
            screen = None
        if isinstance(screen, SubagentScreen):
            await self.pop_screen()
        self._active_child_key = None

        conversation = self.query_one("#conversation", Conversation)
        removal = conversation.remove_children()
        if hasattr(removal, "__await__"):
            await removal
        self._update_accounting_status()

    def _request_key(self, event) -> RequestKey:
        return RequestKey(
            scope=getattr(event, "scope", None),
            subagent_index=getattr(event, "subagent_index", None),
            turn=event.turn,
            iteration=event.iteration,
            request_id=event.request_id,
        )

    def _render_canonical(self, event, *, replay: bool = False) -> None:
        """Render one persisted canonical event, deduplicated by id.

        Replay path only: assistant text arrives as AssistantMessage (no
        streaming deltas) and tool summaries are reconstructed client-side via
        the shared formatters. Advances the replay cursor.
        """
        event_id = getattr(event, "id", None)
        if event_id is not None:
            if event_id in self._seen_event_ids:
                return
            self._seen_event_ids.add(event_id)
            if event.persist:
                self._last_event_id = event_id

        if (
            getattr(event, "scope", None) is not None
            and getattr(event, "subagent_index", None) is not None
        ):
            if isinstance(event, LLMRequest):
                self._accumulate_ledger(event)
            self._handle_scoped_event(event, replay=replay)
            return

        conv = self.query_one("#conversation", Conversation)

        if isinstance(event, ce.UserMessage):
            self._reset_turn_metrics()
            if event.content:
                conv.add_user_message(event.content)
        elif isinstance(event, ce.Handshake):
            status = self.query_one("#status", StatusBar)
            status.session_id = event.session_id
            self._session_id = event.session_id
            if event.protocol_version > PROTOCOL_VERSION and not self._protocol_warned:
                self._protocol_warned = True
                self._show_client_error(
                    f"Protocol version mismatch: session uses v{event.protocol_version}, "
                    f"this client supports v{PROTOCOL_VERSION}. "
                    "Some features may not work — consider updating the CLI."
                )
        elif isinstance(event, ce.SessionStatus):
            status = self.query_one("#status", StatusBar)
            model = load_models().get(event.model_key)
            if model is not None:
                status.model_name = model.name
                status.supports_cache = model.can_cache
            status.git_branch = event.git_branch
        elif isinstance(event, ce.ErrorNotice):
            self._show_client_error(event.message)
            if event.kind in {"turn_active", "turn_error", "storage_error"} and self._turn_active:
                self._end_turn()
        elif isinstance(event, ce.TextDelta):
            key = self._request_key(event)
            if key in self._finalized_assistant_requests:
                return
            self._remove_throbber()
            self._transient_assistant_text[key] = (
                self._transient_assistant_text.get(key, "") + event.text
            )
            if self._streaming is None:
                self._streaming = conv.begin_streaming()
                self._turn_active = True
            self._stream_request_key = key
            self._stream_text = self._transient_assistant_text[key]
            self._streaming.append(event.text)
            self._estimated_output += len(event.text) // 4
            status = self.query_one("#status", StatusBar)
            status.session_output = self._cumulative_output + self._estimated_output
            conv.scroll_if_at_bottom()
        elif isinstance(event, ce.AssistantMessage):
            key = self._request_key(event)
            transient = self._transient_assistant_text.pop(key, None)
            self._finalized_assistant_requests.add(key)
            if self._streaming is not None and self._stream_request_key == key:
                if transient == event.content:
                    self._finalise_streaming()
                else:
                    self._streaming.remove()
                    self._streaming = None
                    self._stream_text = ""
                    self._stream_request_key = None
                    if event.content:
                        conv.add_assistant_message(event.content)
            elif event.content:
                conv.add_assistant_message(event.content)
            if event.interrupted:
                conv.add_cancelled()
        elif isinstance(event, ce.IterationStart):
            # Create a visual block lazily when this iteration contains tools.
            # Text-only iterations should not leave empty blocks in replay.
            self._iteration_block = None
        elif isinstance(event, ce.ToolCall):
            if self._iteration_block is None:
                self._iteration_block = conv.begin_iteration()
            input_summary = format_tool_pending(event.name, event.input)
            self._pending_tool_inputs[event.tool_use_id] = (event.name, event.input)
            entry = self._iteration_block.add_pending(event.tool_use_id, event.name, input_summary)
            if event.name == "task":
                tasks = event.input.get("tasks", [])
                if isinstance(tasks, list):
                    self._parent_task_inputs[event.tool_use_id] = tasks
                    self._parent_task_entries[event.tool_use_id] = entry
        elif isinstance(event, ce.ToolResult):
            if self._iteration_block is not None:
                pending = self._pending_tool_inputs.pop(event.tool_use_id, None)
                if pending is not None:
                    name, tool_input = pending
                    summary = format_tool_complete(
                        name, tool_input, event.content, event.is_error, event.duration_ms
                    )
                else:
                    summary = event.content[:200] if event.content else ""
                self._iteration_block.complete_tool(
                    event.tool_use_id,
                    event.is_error,
                    event.duration_ms,
                    event.result_lines,
                    event.result_bytes,
                    summary,
                )
        elif isinstance(event, ce.LLMRequest):
            self._accumulate_ledger(event)
            if event.scope is None and event.subagent_index is None:
                self._turn_input += event.input_tokens
                self._turn_cache_read += event.cache_read_tokens
                self._turn_cache_write += event.cache_write_tokens
                self._turn_output += event.output_tokens
                self._turn_cost += event.cost_usd
                self._turn_duration_s += event.duration_ms / 1000
        elif isinstance(event, ce.TurnError):
            conv.add_error(event.message)
            self._iteration_block = None
            self._end_turn()
        elif isinstance(event, ce.TurnInterrupted):
            conv.add_cancelled()
            self._iteration_block = None
            self._end_turn()
        elif isinstance(event, ce.TurnComplete):
            self._show_turn_status()
            self._iteration_block = None
            self._end_turn()

    def _accumulate_ledger(self, event: LLMRequest) -> None:
        """Fold an llm_request ledger event into cumulative accounting.

        This is the authoritative live cost/token source (matches the persisted
        ledger). Deduplicated by event id so replay + live can't double-count.
        Updates the status bar.
        """
        if event.id in self._seen_accounted_ids:
            return
        self._seen_accounted_ids.add(event.id)
        self._estimated_output = 0
        self._cumulative_input += event.input_tokens
        self._cumulative_output += event.output_tokens
        self._cumulative_cache_read += event.cache_read_tokens
        self._cumulative_cache_write += event.cache_write_tokens
        if event.scope is None and event.subagent_index is None:
            self._latest_context_tokens = event.context_tokens
            self._latest_context_pct = self._estimate_context_pct(
                event.context_tokens, event.model_key
            )
        self._cumulative_cost += event.cost_usd
        self._update_accounting_status()

    def _estimate_context_pct(self, context_tokens: int, model_key: str) -> float:
        """Calculate context usage from a persisted request."""
        model = load_models().get(model_key)
        if model is None or model.context <= 0:
            return 0.0
        return (context_tokens / model.context) * 100

    def _update_accounting_status(self) -> None:
        """Push cumulative accounting to the status bar."""
        status = self.query_one("#status", StatusBar)
        status.session_input = self._cumulative_input
        status.session_output = self._cumulative_output
        status.cache_read = self._cumulative_cache_read
        status.cache_write = self._cumulative_cache_write
        status.context_tokens = self._latest_context_tokens
        status.context_pct = self._latest_context_pct
        status.pricing_label = f"${self._cumulative_cost:.4f}"

    async def _receive_loop(self) -> None:
        """Consume events from the WebSocket and dispatch to widget updates.

        During the buffering phase (before history is fetched), events are
        stored in _event_buffer. After reconciliation, events are dispatched
        directly.

        When the generator ends because the connection dropped, we surface a
        client error and kick off a reconnect attempt. A clean shutdown
        (quit) does neither.
        """
        try:
            async for event in self._ws.receive():
                if self._buffering:
                    self._event_buffer.append(event)
                else:
                    self._apply_event(event)
        except Exception as e:
            log.warning("WS receive loop error: %s", e)
            if not self._shutting_down:
                self._show_client_error(f"Connection lost: {e}")
                close_code = getattr(getattr(e, "rcvd", None), "code", None)
                self._schedule_reconnect(close_code)
            return

        # Generator ended without raising — connection closed underneath us.
        if not self._shutting_down:
            self._show_client_error("Connection lost: the agent closed the stream.")
            self._schedule_reconnect(None)

    def _schedule_reconnect(self, close_code: int | None = None) -> None:
        """Start a background reconnect task if one isn't already running."""
        if self._reconnecting or self._shutting_down:
            return
        self._reconnecting = True
        asyncio.create_task(self._reconnect(close_code))

    async def _reconnect(self, close_code: int | None = None) -> None:
        """Reconnect to the agent with backoff and resync via canonical replay.

        Close-code handling:
        - 4004 (session not found): give up immediately.
        - 4002 (backend unreachable) / 1006 (abnormal) / None: retry with backoff.

        On success, restarts the receive loop and replays any missed canonical
        events via /events?after=<last event id> (deduplicated by event id).

        Total retry window is 30s before giving up.
        """
        if close_code == 4004:
            self._show_client_error("Session has ended. Relaunch to start a new session.")
            self._reconnecting = False
            return

        start = asyncio.get_event_loop().time()
        deadline = start + 30.0
        delays = [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
        try:
            for attempt, delay in enumerate(delays, start=1):
                if self._shutting_down:
                    return
                if asyncio.get_event_loop().time() + delay > deadline:
                    break
                self.notify("Reconnecting...", timeout=delay)
                await asyncio.sleep(delay)
                try:
                    await self._ws.connect(self._ws_url)
                except Exception as e:  # noqa: BLE001 — retry on any connect failure
                    log.warning("Reconnect attempt %d failed: %s", attempt, e)
                    continue

                # Reconnected — buffer incoming events, replay missed canonical
                # events (deduped by id), then dispatch buffered live events.
                self._event_buffer = []
                self._buffering = True
                # Cancel any stale receive loop before starting a fresh one.
                if self._receive_task is not None and not self._receive_task.done():
                    self._receive_task.cancel()
                    try:
                        await self._receive_task
                    except asyncio.CancelledError:
                        pass
                self._receive_task = asyncio.create_task(self._receive_loop())
                if not await self._replay_events():
                    self._buffering = False
                    self._event_buffer = []
                    if self._receive_task is not None and not self._receive_task.done():
                        self._receive_task.cancel()
                        try:
                            await self._receive_task
                        except asyncio.CancelledError:
                            pass
                    await self._ws.disconnect()
                    continue
                self._buffering = False
                self._dispatch_buffered_events()
                self._event_buffer = []
                self.notify("Reconnected to agent")
                return

            self._show_client_error("Reconnect failed after 30s. Relaunch the client to continue.")
        finally:
            self._reconnecting = False

    def _dispatch_buffered_events(self) -> None:
        """Flush live frames after history; request keys suppress duplicates."""
        for event in self._event_buffer:
            self._apply_event(event)

    def _apply_event(self, event) -> None:
        """Apply one live or historical event through the imperative UI path."""
        self._render_canonical(event)

    def _handle_scoped_event(self, event, *, replay: bool = False) -> bool:
        """Reduce one canonical/live child event into shared child state."""
        scope = getattr(event, "scope", None)
        index = getattr(event, "subagent_index", None)
        if scope is None or index is None:
            return False
        child = self._child(scope, index)
        if isinstance(event, LLMRequest):
            child.cost += event.cost_usd
            child.context_tokens = event.context_tokens
            child.set_activity("Thinking...")
        elif isinstance(event, (IterationStart, ce.IterationStart)):
            child.set_activity("Thinking...")
        elif isinstance(event, (TextDelta, ce.TextDelta)):
            key = self._request_key(event)
            if key in self._finalized_assistant_requests:
                return True
            text = self._transient_assistant_text.get(key, "") + event.text
            self._transient_assistant_text[key] = text
            child.activity = text[-512:] or "Responding..."
        elif isinstance(event, ce.AssistantMessage):
            key = self._request_key(event)
            self._transient_assistant_text.pop(key, None)
            self._finalized_assistant_requests.add(key)
            if event.content:
                child.add_line(event.content)
            child.activity = "Responding..."
        elif isinstance(event, (ToolCall, ce.ToolCall)):
            summary = format_tool_activity(event.name, event.input)
            self._child_pending_tools[(scope, index, event.tool_use_id)] = (
                event.name,
                event.input,
            )
            child.set_activity(summary)
        elif isinstance(event, (ToolResult, ce.ToolResult)):
            pending = self._child_pending_tools.pop((scope, index, event.tool_use_id), None)
            name, tool_input = pending or ("tool", {})
            result_summary = format_tool_complete(
                name, tool_input, event.content, event.is_error, event.duration_ms
            )
            child.add_line(result_summary)
            if event.is_error:
                child.activity = result_summary
            else:
                child.activity = "Thinking..."
        elif isinstance(event, (TurnComplete, ce.TurnComplete)):
            child.status = "complete"
            child.activity = "Completed"
        elif isinstance(event, (TurnInterrupted, ce.TurnInterrupted)):
            child.status = "interrupted"
            child.activity = "interrupted"
        elif isinstance(event, (TurnError, ce.TurnError)):
            child.status = "error"
            child.error = getattr(event, "message", "error")
            child.activity = child.error
        self._render_child(child)
        self._update_child_modal(child)
        return True

    def _render_child(self, child: ChildActivity) -> None:
        """Mount or update the collapsed child activity widget."""
        conv = self.query_one("#conversation", Conversation)
        widget_id = f"child-{child.scope}-{child.index}".replace(" ", "-")
        try:
            widget = conv.query_one(f"#{widget_id}", SubagentActivity)
            widget.update_state()
        except Exception:
            widget = SubagentActivity(child, key=widget_id)
            parent = self._parent_task_entries.get(child.scope)
            if parent is not None:
                parent.add_child(widget)
            else:
                conv.mount(widget)
        self._child_widgets[(child.scope, child.index)] = widget

    def _update_child_modal(self, child: ChildActivity) -> None:
        if self._active_child_key != (child.scope, child.index):
            return
        try:
            self.screen.update_state()
        except AttributeError:
            pass

    def open_child_detail(self, key: tuple[str, int]) -> None:
        """Open a live detail modal for a known child."""
        child = self._child_activity.get(key)
        if child is None:
            return
        self._active_child_key = key
        self.push_screen(SubagentScreen(child))

    async def action_subagent_picker(self) -> None:
        """Open the command palette filtered to subagent entries."""
        self.action_command_palette()

    def action_stop_child(self) -> None:
        """Target-stop the selected child while leaving siblings active."""
        if self._active_child_key is not None:
            asyncio.create_task(
                self._ws.send_command(
                    InterruptCommand(
                        scope=self._active_child_key[0], subagent_index=self._active_child_key[1]
                    )
                )
            )

    def _child(self, scope: str, index: int) -> ChildActivity:
        """Return or create live state for one scoped child."""
        key = (scope, index)
        child = self._child_activity.get(key)
        if child is None:
            child = ChildActivity(scope=scope, index=index)
            tasks = self._parent_task_inputs.get(scope, [])
            if index < len(tasks) and isinstance(tasks[index], dict):
                child.agent = str(tasks[index].get("agent", "child"))
            self._child_activity[key] = child
        self._render_child(child)
        return child

    # --- Message flow ---

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Handle user message submission."""
        content = event.content

        # Direct shell: ! prefix bypasses the LLM entirely
        if content.startswith("!"):
            command = content[1:].strip()
            if not command:
                return
            if self._shell_active:
                conv = self.query_one("#conversation", Conversation)
                conv.add_client_error("Shell command already running")
                return
            asyncio.create_task(self._run_direct_shell(command))
            return

        if self._turn_active:
            return

        content = event.content
        self._turn_active = True
        self._turn_started_at = time.monotonic()
        self._turn_input = 0
        self._turn_cache_read = 0
        self._turn_cache_write = 0
        self._turn_output = 0
        self._turn_cost = 0.0
        self._turn_duration_s = 0.0
        # Show throbber while waiting
        self._show_throbber()

        self.query_one("#input", MessageInput).disabled = True
        self._stream_text = ""
        self._streaming = None

        # Send via WebSocket (fire-and-forget task)
        asyncio.create_task(self._send_message(content))

    async def _send_message(self, content: str) -> None:
        """Send a message to the agent via WebSocket."""
        try:
            await self._ws.send_message(content)
        except Exception as e:
            self._show_client_error(f"Send failed: {e}")
            self._end_turn()
            # A send failure means the socket is dead; try to recover.
            self._schedule_reconnect()

    async def _run_direct_shell(self, command: str) -> None:
        """Execute and render a command locally through host-side docker exec."""
        conv = self.query_one("#conversation", Conversation)
        self._shell_active = True
        self._shell_command = command
        self._shell_cancel_requested = False
        max_output_lines = 10_000
        timeout = 30

        try:
            self._shell_proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "-w",
                "/workspace",
                self._container_name,
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    self._shell_proc.communicate(), timeout=timeout
                )
                exit_code = (
                    130 if self._shell_cancel_requested else (self._shell_proc.returncode or 0)
                )
                output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            except TimeoutError:
                self._shell_proc.kill()
                await self._shell_proc.wait()
                exit_code = 124
                output = "(timed out)"

            lines = output.split("\n")
            if len(lines) > max_output_lines:
                output = "\n".join(lines[:max_output_lines]) + "\n(truncated)"
            conv.add_shell_output(command, output, exit_code=exit_code)
        except Exception as exc:  # noqa: BLE001 — local execution failure
            conv.add_client_error(f"Docker exec failed: {exc}")
        finally:
            self._shell_active = False
            self._shell_proc = None
            self._shell_command = ""
            self._shell_cancel_requested = False

    # --- UI helpers ---

    def _reset_turn_metrics(self) -> None:
        """Reset client-only accounting for the next root turn."""
        self._turn_started_at = None
        self._turn_input = 0
        self._turn_cache_read = 0
        self._turn_cache_write = 0
        self._turn_output = 0
        self._turn_cost = 0.0
        self._turn_duration_s = 0.0

    def _show_turn_status(self) -> None:
        """Render client-only metrics for the completed root turn."""
        if self._turn_started_at is None and not any(
            (
                self._turn_input,
                self._turn_cache_read,
                self._turn_cache_write,
                self._turn_output,
                self._turn_cost,
            )
        ):
            return
        duration_s = (
            time.monotonic() - self._turn_started_at
            if self._turn_started_at is not None
            else self._turn_duration_s
        )
        conv = self.query_one("#conversation", Conversation)
        conv.add_turn_status(
            duration_s,
            self._turn_input,
            self._turn_cache_read,
            self._turn_cache_write,
            self._turn_output,
            self._turn_cost,
        )
        self._turn_started_at = None

    def _end_turn(self) -> None:
        """Single teardown path for every turn outcome."""
        self._remove_throbber()
        self._finalise_streaming()
        conv = self.query_one("#conversation", Conversation)
        conv.end_iteration()
        self._iteration_block = None
        self._turn_active = False
        inp = self.query_one("#input", MessageInput)
        inp.disabled = False
        inp.focus()

    def _finalise_streaming(self) -> None:
        """Convert the active streaming message to a completed markdown block."""
        if self._streaming is None:
            return
        conv = self.query_one("#conversation", Conversation)
        if self._stream_text:
            conv.finalise_streaming(self._streaming)
        else:
            self._streaming.remove()
        self._streaming = None
        self._stream_text = ""
        self._stream_request_key = None

    def _show_throbber(self) -> None:
        """Show the fixed throbber while the agent is thinking."""
        if self._throbber is not None or not self._turn_active:
            return
        self._throbber = self.query_one("#throbber", Throbber)
        self._throbber.display = True

    def _remove_throbber(self) -> None:
        """Hide the throbber animation widget."""
        if self._throbber is not None:
            self._throbber.display = False
            self._throbber = None
        else:
            self.query_one("#throbber", Throbber).display = False

    def _show_error(self, message: str) -> None:
        """Display an agent/server error message in the conversation."""
        conv = self.query_one("#conversation", Conversation)
        conv.add_error(message)

    def _show_client_error(self, message: str) -> None:
        """Display a client/transport error, marked as local to this client.

        These originate in the TUI or WebSocket transport and are NOT recorded
        in the session log, so they are styled distinctly from agent errors.
        """
        conv = self.query_one("#conversation", Conversation)
        conv.add_client_error(message)

    # --- Actions ---

    def action_copy_block(self) -> None:
        """Copy the currently focused message block to clipboard."""
        focused = self.focused
        if focused is not None and hasattr(focused, "get_copy_text"):
            text = focused.get_copy_text()
            if text:
                self.copy_to_clipboard(text)
                self.notify("Copied to clipboard")

    def action_cancel(self) -> None:
        """Esc — signal the agent to interrupt, or kill running shell command.

        When idle, double-tap within 500ms clears the input.
        """
        if self._shell_active and self._shell_proc is not None:
            self._shell_cancel_requested = True
            self._shell_proc.kill()
            return
        if self._turn_active:
            asyncio.create_task(self._ws.send_interrupt())
            self._last_esc_time = 0
        else:
            now = time.monotonic()
            inp = self.query_one("#input", MessageInput)
            if self._last_esc_time and (now - self._last_esc_time) < 0.5 and inp.text:
                inp.clear()
                self._last_esc_time = 0
            else:
                self._last_esc_time = now

    async def action_quit(self) -> None:
        """Graceful shutdown: disconnect WS and exit."""
        self._shutting_down = True
        if self._receive_task is not None:
            self._receive_task.cancel()
        await self._ws.disconnect()
        await super().action_quit()

    def action_editor(self) -> None:
        """Open $EDITOR for message composition. Auto-submits on save."""
        if self._turn_active:
            return

        editor = os.environ.get("EDITOR", "nano")
        inp = self.query_one("#input", MessageInput)
        current_text = inp.text

        # Write current input to a tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, prefix="archie-")
        try:
            tmp.write(current_text)
            tmp.close()
            mtime_before = os.path.getmtime(tmp.name)

            editor_error: str | None = None
            with self.suspend():
                try:
                    subprocess.run([editor, tmp.name], check=False)
                except FileNotFoundError:
                    editor_error = f"Editor not found: {editor}"

            if editor_error:
                self.notify(editor_error, severity="error")
                return

            mtime_after = os.path.getmtime(tmp.name)
            if mtime_after == mtime_before:
                # Editor exited without saving — no-op
                return

            with open(tmp.name) as f:
                content = f.read().strip()

            if content:
                inp.clear()
                inp._history.append(content)
                inp._history_idx = len(inp._history)
                inp._draft = ""
                inp.post_message(MessageInput.Submitted(content))
            else:
                inp.clear()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def switch_model(self, model_key: str) -> None:
        """Send a model switch command to the agent (called by ModelProvider)."""
        if self._turn_active:
            self.notify("Cannot switch model during active turn", severity="warning")
            return
        asyncio.create_task(self._ws.send_command(SwitchModelCommand(model_key=model_key)))
