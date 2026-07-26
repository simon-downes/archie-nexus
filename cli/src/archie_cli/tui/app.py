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
    IterationStart,
    ModelSwitched,
    SessionInfo,
    StatusUpdated,
    SwitchModelCommand,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    Usage,
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
        # Highest turn_index already rendered — used for history dedup on reconnect
        self._last_displayed_turn: int = 0

        # UI state
        self._streaming: StreamingMessage | None = None
        self._stream_text: str = ""
        self._turn_active: bool = False
        self._throbber: Throbber | None = None
        self._iteration_block: IterationBlock | None = None
        self._last_esc_time: float = 0.0

        # Session state for local accumulation
        self._session_id: str = ""
        self._cumulative_input: int = 0
        self._cumulative_output: int = 0
        self._cumulative_cache_read: int = 0
        self._cumulative_cache_write: int = 0
        self._cumulative_cost: float = 0.0
        self._cost_per_m_input: float = 0.0
        self._cost_per_m_output: float = 0.0
        self._cost_per_m_cache_read: float = 0.0
        self._cost_per_m_cache_write: float = 0.0

        # Live output estimation (chars/4, reconciled on Usage)
        self._estimated_output: int = 0

        # Direct shell (! prefix) state
        self._shell_active: bool = False
        self._shell_proc: asyncio.subprocess.Process | None = None
        self._shell_command: str = ""

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
            await self._ws.connect(self._ws_url)
        except Exception as e:
            self._show_client_error(f"Connection failed: {e}")
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
        return await self._load_history_since(0)

    async def _load_history_since(self, since: int) -> int:
        """Fetch history and render only turns with turn_index > since.

        Returns the highest turn_index rendered (or since if none rendered).
        """
        conv = self.query_one("#conversation", Conversation)
        last_turn_index = since
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._api_url}/history", timeout=5.0)
                if resp.status_code != 200:
                    return since
                turns = resp.json()
        except Exception as e:
            log.warning("Failed to load history: %s", e)
            return since

        for turn in turns:
            role = turn.get("role")
            turn_index = turn.get("turn_index", 0)
            last_turn_index = max(last_turn_index, turn_index)
            if turn_index <= since:
                continue  # already displayed — skip
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

        self._last_displayed_turn = last_turn_index
        return last_turn_index

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
                if isinstance(event, (SessionInfo, ModelSwitched, StatusUpdated)):
                    # Session-level events have no turn_index — always dispatch immediately
                    self._handle_event(event)
                elif self._buffering:
                    self._event_buffer.append(event)
                else:
                    self._handle_event(event)
        except Exception as e:
            log.warning("WS receive loop error: %s", e)
            if not self._shutting_down:
                self._show_client_error(f"Connection lost: {e}")
                self._end_turn()
                close_code = getattr(getattr(e, "rcvd", None), "code", None)
                self._schedule_reconnect(close_code)
            return

        # Generator ended without raising — connection closed underneath us.
        if not self._shutting_down:
            self._show_client_error("Connection lost: the agent closed the stream.")
            self._end_turn()
            self._schedule_reconnect(None)

    def _schedule_reconnect(self, close_code: int | None = None) -> None:
        """Start a background reconnect task if one isn't already running."""
        if self._reconnecting or self._shutting_down:
            return
        self._reconnecting = True
        asyncio.create_task(self._reconnect(close_code))

    async def _reconnect(self, close_code: int | None = None) -> None:
        """Reconnect to the agent with backoff and resync via /history.

        Close-code handling:
        - 4004 (session not found): give up immediately.
        - 4002 (backend unreachable) / 1006 (abnormal) / None: retry with backoff.

        On success, restarts the receive loop and replays any missed turns
        (deduplicated by turn_index).

        Total retry window is 30s before giving up.
        """
        if close_code == 4004:
            self._show_client_error("Session has ended. Relaunch to start a new session.")
            self._reconnecting = False
            return

        start = asyncio.get_event_loop().time()
        deadline = start + 30.0
        delays = [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
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

                # Reconnected — buffer incoming events, resync history (deduped), replay.
                self._event_buffer = []
                self._buffering = True
                self._receive_task = asyncio.create_task(self._receive_loop())
                since = self._last_displayed_turn
                last_turn_index = await self._load_history_since(since)
                self._buffering = False
                for event in self._event_buffer:
                    if getattr(event, "turn_index", 0) > last_turn_index:
                        self._handle_event(event)
                self._event_buffer = []
                self.notify("Reconnected to agent")
                return

            self._show_client_error(
                "Reconnect failed after 30s. "
                "Relaunch the client to continue."
            )
        finally:
            self._reconnecting = False

    def _handle_event(self, event) -> None:
        """Dispatch one server event to the appropriate widget update."""
        conv = self.query_one("#conversation", Conversation)

        if isinstance(event, SessionInfo):
            status = self.query_one("#status", StatusBar)
            status.session_id = event.session_id
            status.model_name = event.model
            status.git_branch = event.git_branch
            # Store cost rates for local cost computation
            self._session_id = event.session_id
            self._cost_per_m_input = event.cost_per_m_input
            self._cost_per_m_output = event.cost_per_m_output
            self._cost_per_m_cache_read = event.cost_per_m_cache_read
            self._cost_per_m_cache_write = event.cost_per_m_cache_write

        elif isinstance(event, ModelSwitched):
            status = self.query_one("#status", StatusBar)
            status.model_name = event.model_name
            status.supports_cache = event.supports_cache
            # Update cost rates for future Usage events
            self._cost_per_m_input = event.cost_per_m_input
            self._cost_per_m_output = event.cost_per_m_output
            self._cost_per_m_cache_read = event.cost_per_m_cache_read
            self._cost_per_m_cache_write = event.cost_per_m_cache_write
            self.notify(f"Switched to {event.model_name}")

        elif isinstance(event, StatusUpdated):
            status = self.query_one("#status", StatusBar)
            status.git_branch = event.git_branch

        elif isinstance(event, IterationStart):
            # Deterministic block boundary: finalise any in-progress streaming
            # and reset the iteration block so the next TextDelta/ToolCall opens
            # a fresh visual block. Decoupled from Usage metadata.
            if self._streaming is not None:
                self._finalise_streaming()
            self._iteration_block = None

        elif isinstance(event, TextDelta):
            self._remove_throbber()
            if self._streaming is None:
                self._streaming = conv.begin_streaming()
                self._turn_active = True
            self._stream_text += event.text
            self._streaming.append(event.text)
            # Live output estimation
            self._estimated_output += len(event.text) // 4
            status = self.query_one("#status", StatusBar)
            status.session_output = self._cumulative_output + self._estimated_output
            conv.scroll_end(animate=False)

        elif isinstance(event, Usage):
            # Accumulate per-request values locally
            self._cumulative_input += event.input_tokens
            self._cumulative_output += event.output_tokens
            self._cumulative_cache_read += event.cache_read_tokens
            self._cumulative_cache_write += event.cache_write_tokens
            # Reset output estimation — reconcile with real value
            self._estimated_output = 0
            # Compute cost delta for this request using current rates
            from archie_shared.models import CostConfig, calculate_cost

            cost_config = CostConfig(
                input=self._cost_per_m_input,
                output=self._cost_per_m_output,
                cache_read=self._cost_per_m_cache_read,
                cache_write=self._cost_per_m_cache_write,
            )
            self._cumulative_cost += calculate_cost(
                cost_config,
                event.input_tokens,
                event.output_tokens,
                event.cache_read_tokens,
                event.cache_write_tokens,
            )
            # Update status bar with real values
            status = self.query_one("#status", StatusBar)
            status.session_input = self._cumulative_input
            status.session_output = self._cumulative_output
            status.cache_read = self._cumulative_cache_read
            status.cache_write = self._cumulative_cache_write
            status.pricing_label = f"${self._cumulative_cost:.4f}"
            status.context_pct = event.context_pct

        elif isinstance(event, TurnComplete):
            self._end_turn()

        elif isinstance(event, TurnInterrupted):
            conv.add_error("[interrupted]")
            self._end_turn()

        elif isinstance(event, TurnError):
            self._show_error(event.message)
            self._end_turn()

        elif isinstance(event, ToolCall):
            self._remove_throbber()
            # Finalise any in-progress streaming text before showing tool activity
            if self._streaming is not None:
                self._finalise_streaming()
            # Start a new iteration block if needed
            if self._iteration_block is None:
                self._iteration_block = conv.begin_iteration()
            # Add pending entry
            self._iteration_block.add_pending(event.tool_use_id, event.name, event.input_summary)
            conv.scroll_end(animate=False)

        elif isinstance(event, ToolResult):
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
            self._show_client_error(f"Send failed: {e}")
            self._end_turn()
            # A send failure means the socket is dead; try to recover.
            self._schedule_reconnect()

    async def _run_direct_shell(self, command: str) -> None:
        """Execute a command in the session container via docker exec.

        Runs async so the TUI stays responsive. Output is displayed in a
        ShellOutput widget. Errors (docker exec failure) use ClientErrorMessage.
        """
        conv = self.query_one("#conversation", Conversation)
        self._shell_active = True
        self._shell_command = command
        max_output_lines = 10_000
        timeout = 30

        try:
            cname = self._container_name
            self._shell_proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-w", "/workspace", cname, "bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    self._shell_proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                self._shell_proc.kill()
                await self._shell_proc.wait()
                conv.add_shell_output(command, "(timed out)", exit_code=124)
                self._log_shell(command, 124, "(timed out)")
                return

            exit_code = self._shell_proc.returncode or 0
            output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""

            # Truncate large output
            lines = output.split("\n")
            if len(lines) > max_output_lines:
                output = "\n".join(lines[:max_output_lines]) + "\n(truncated)"

            conv.add_shell_output(command, output.rstrip(), exit_code=exit_code)
            self._log_shell(command, exit_code, output)

        except Exception as e:
            conv.add_client_error(f"Docker exec failed: {e}")
        finally:
            self._shell_active = False
            self._shell_proc = None
            self._shell_command = ""

    def _log_shell(self, command: str, exit_code: int, output: str) -> None:
        """Best-effort POST to /shell endpoint to log command in session."""
        asyncio.create_task(self._log_shell_async(command, exit_code, output))

    async def _log_shell_async(self, command: str, exit_code: int, output: str) -> None:
        """Async POST to /shell endpoint."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self._api_url}/shell",
                    json={"command": command, "exit_code": exit_code, "output": output},
                    timeout=5,
                )
        except Exception:
            pass  # Best-effort

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
            self._shell_proc.kill()
            conv = self.query_one("#conversation", Conversation)
            cmd = self._shell_command or "?"
            conv.add_shell_output(cmd, "(interrupted)", exit_code=130)
            self._log_shell(cmd, 130, "(interrupted)")
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
