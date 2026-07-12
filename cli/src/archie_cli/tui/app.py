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

import httpx
from archie_shared.events import (
    ModelSwitched,
    SessionInfo,
    StatusUpdated,
    SwitchModelCommand,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
)
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from archie_cli.tui import theme
from archie_cli.tui.conversation import Conversation, IterationBlock, StreamingMessage
from archie_cli.tui.input import MessageInput
from archie_cli.tui.models_provider import ModelProvider
from archie_cli.tui.status import StatusBar
from archie_cli.tui.throbber import Throbber
from archie_cli.ws_client import WSClient

log = logging.getLogger(__name__)


class ArchieApp(App):
    """TUI client that attaches to a running agent session via WebSocket."""

    TITLE = "Archie"
    CSS_PATH = "archie.tcss"
    COMMANDS = App.COMMANDS | {ModelProvider}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "copy_block", "Copy Block"),
        Binding("ctrl+g", "editor", "Editor", show=False),
    ]

    def __init__(self, host: str, port: int) -> None:
        super().__init__()

        # Register custom theme
        self.register_theme(theme.THEME)
        self.theme = "archie"

        self._host = host
        self._port = port
        self._ws = WSClient()
        self._receive_task: asyncio.Task | None = None

        # UI state
        self._streaming: StreamingMessage | None = None
        self._stream_text: str = ""
        self._turn_active: bool = False
        self._throbber: Throbber | None = None
        self._iteration_block: IterationBlock | None = None
        self._last_esc_time: float = 0.0

    def compose(self) -> ComposeResult:
        """Build the main UI layout."""
        yield Conversation(id="conversation")
        yield StatusBar(id="status")
        yield MessageInput(id="input")
        yield Footer()

    async def on_mount(self) -> None:
        """Connect to agent and start receiving events.

        Ordering per plan: subscribe WS first → start receive loop (buffering) →
        fetch /history → reconcile by turn_index → replay remainder.
        """
        self.query_one("#input", MessageInput).focus()

        try:
            ws_url = f"ws://{self._host}:{self._port}/stream"
            await self._ws.connect(ws_url)
        except Exception as e:
            self._show_error(f"Connection failed: {e}")
            return

        # Start receive loop FIRST — events buffer in _event_buffer
        self._event_buffer: list = []
        self._buffering = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        # Fetch history while receive loop buffers incoming events
        last_turn_index = await self._load_history()

        # Reconcile: discard buffered events with turn_index ≤ last history turn
        self._buffering = False
        for event in self._event_buffer:
            turn_index = getattr(event, "turn_index", 0)
            if turn_index > last_turn_index:
                self._handle_event(event)
        self._event_buffer = []

    async def _load_history(self) -> int:
        """Fetch conversation history and populate the conversation.

        Returns the highest turn_index in the history (0 if empty).
        """
        conv = self.query_one("#conversation", Conversation)
        last_turn_index = 0
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://{self._host}:{self._port}/history", timeout=5.0)
                if resp.status_code != 200:
                    return 0
                turns = resp.json()
        except Exception as e:
            log.warning("Failed to load history: %s", e)
            return 0

        for turn in turns:
            role = turn.get("role")
            turn_index = turn.get("turn_index", 0)
            last_turn_index = max(last_turn_index, turn_index)
            content_blocks = turn.get("content", [])
            # Extract text blocks only for display
            text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
            text = "\n".join(text_parts) if text_parts else ""

            if role == "user":
                if text:
                    conv.add_user_message(text)
            elif role == "assistant":
                if text:
                    conv.add_assistant_message(text)
            elif role == "error":
                conv.add_error(text or "Unknown error")
            elif role == "interrupted":
                conv.add_error("[interrupted]")

        return last_turn_index

    async def _receive_loop(self) -> None:
        """Consume events from the WebSocket and dispatch to widget updates.

        During the buffering phase (before history is fetched), events are
        stored in _event_buffer. After reconciliation, events are dispatched
        directly.
        """
        try:
            async for event in self._ws.receive():
                if isinstance(event, (SessionInfo, ModelSwitched, StatusUpdated)):
                    # Session-level events have no turn_index — always dispatch immediately
                    self._handle_event(event)
                elif self._buffering:
                    self._event_buffer.append(event)
                else:
                    self._handle_event(event)
        except Exception as e:
            log.warning("WS receive loop error: %s", e)
            self._show_error(f"Connection lost: {e}")

    def _handle_event(self, event) -> None:
        """Dispatch one server event to the appropriate widget update."""
        conv = self.query_one("#conversation", Conversation)

        if isinstance(event, SessionInfo):
            status = self.query_one("#status", StatusBar)
            status.session_id = event.session_id
            status.model_name = event.model
            status.git_branch = event.git_branch

        elif isinstance(event, ModelSwitched):
            status = self.query_one("#status", StatusBar)
            status.model_name = event.model_name
            status.supports_cache = event.supports_cache
            self.notify(f"Switched to {event.model_name}")

        elif isinstance(event, StatusUpdated):
            status = self.query_one("#status", StatusBar)
            status.git_branch = event.git_branch

        elif isinstance(event, TextDeltaEvent):
            self._remove_throbber()
            if self._streaming is None:
                self._streaming = conv.begin_streaming()
                self._turn_active = True
            self._stream_text += event.text
            self._streaming.append(event.text)
            conv.scroll_end(animate=False)

        elif isinstance(event, UsageUpdated):
            status = self.query_one("#status", StatusBar)
            status.session_input = event.input_tokens
            status.session_output = event.output_tokens
            status.cache_read = event.cache_read_tokens
            status.cache_write = event.cache_write_tokens
            status.pricing_label = f"${event.cost:.4f}"
            status.context_pct = event.context_pct

        elif isinstance(event, TurnComplete):
            self._end_turn()

        elif isinstance(event, TurnInterrupted):
            conv.add_error("[interrupted]")
            self._end_turn()

        elif isinstance(event, TurnError):
            self._show_error(event.message)
            self._end_turn()

        elif isinstance(event, ToolCallEvent):
            self._remove_throbber()
            # Finalise any in-progress streaming text before showing tool activity
            # Also reset the iteration block — new text output means a new iteration
            if self._streaming is not None:
                self._finalise_streaming()
                self._iteration_block = None
            # Start a new iteration block if needed
            if self._iteration_block is None:
                self._iteration_block = conv.begin_iteration()
            # Add pending entry showing source code
            self._iteration_block.add_pending(event.tool_use_id, event.name, event.input_summary)
            conv.scroll_end(animate=False)

        elif isinstance(event, ToolResultEvent):
            if self._iteration_block is not None:
                self._iteration_block.complete_tool(
                    event.tool_use_id,
                    event.is_error,
                    event.duration_ms,
                    event.result_bytes,
                    event.summary,
                )
                conv.scroll_end(animate=False)

    # --- Message flow ---

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Handle user message submission."""
        if self._turn_active:
            return

        content = event.content
        self._turn_active = True
        conv = self.query_one("#conversation", Conversation)
        conv.add_user_message(content)

        # Show throbber while waiting
        self._throbber = Throbber()
        conv.mount(self._throbber)
        self.call_after_refresh(conv.scroll_end, animate=False)

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
            self._show_error(f"Send failed: {e}")
            self._end_turn()

    # --- UI helpers ---

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

    def _remove_throbber(self) -> None:
        """Remove the throbber animation widget."""
        if self._throbber is not None:
            self._throbber.remove()
            self._throbber = None

    def _show_error(self, message: str) -> None:
        """Display an error message in the conversation."""
        conv = self.query_one("#conversation", Conversation)
        conv.add_error(message)

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
        """Esc — signal the agent to interrupt.

        When idle, double-tap within 500ms clears the input.
        """
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
