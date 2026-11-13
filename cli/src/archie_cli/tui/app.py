"""Main Textual application for the attach TUI.

``ArchieApp`` shows one session. The session's state and behaviour live in a
``SessionController`` (``session.py``); the app composes the widgets, renders what the
controller reports (it is the controller's ``SessionListener``) and forwards input and
actions to it. Detail popups, the command palette, the quit prompt, the external editor
and copy stay here.

Threading model: Textual runs an asyncio event loop on the main thread; the
controller's receive loop runs as a task on the same loop, so its notifications update
widgets directly.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time

from archie_shared.commands import AnswerCommand
from archie_shared.protocol import API_PREFIX
from archie_shared.transcript import Change
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from archie_cli.tui import theme
from archie_cli.tui.client import HttpOrchestrator, OrchestratorClient, OrchestratorError
from archie_cli.tui.conversation import format_cost
from archie_cli.tui.detail import DetailScreen, SubagentScreen, ToolDetailScreen
from archie_cli.tui.input import MessageInput
from archie_cli.tui.models_provider import ModelProvider, SubagentProvider
from archie_cli.tui.question import QuestionPanel
from archie_cli.tui.quit_screen import QuitScreen
from archie_cli.tui.session import (
    ClientErrorEntry,
    LocalEntry,
    SessionController,
    TurnStatusEntry,
    estimate_context_pct,
)
from archie_cli.tui.status import StatusBar
from archie_cli.tui.throbber import Throbber, ThrobberContainer
from archie_cli.tui.transcript_view import Conversation, SubagentClicked, ToolClicked
from archie_cli.ws_client import WSClient


class _AttachedSession:
    """``OrchestratorClient`` for today's entry points (M1 only).

    ``archie start``/``attach`` pass ready-made stream and session URLs, and the app
    keeps one ``WSClient`` for its lifetime, reconnecting it in place.
    """

    def __init__(self, base_url: str, ws_url: str) -> None:
        self._http = HttpOrchestrator(base_url)
        self._ws_url = ws_url
        self.ws = WSClient()

    async def events(self, session_id: str, after: str | None) -> str:
        return await self._http.events(session_id, after)

    async def stream(self, session_id: str) -> WSClient:
        await self.ws.connect(self._ws_url)
        return self.ws

    async def delete(self, session_id: str) -> int:
        return await self._http.delete(session_id)


def _split_session_url(api_url: str) -> tuple[str, str]:
    """Split ``<base>/api/sessions/<id>`` into the orchestrator base URL and the id."""
    base, marker, session_id = api_url.rpartition(f"{API_PREFIX}/sessions/")
    if not marker:
        return api_url.rstrip("/"), ""
    return base, session_id.strip("/")


def _forward(name: str, doc: str) -> property:
    """A read/write property for a controller attribute (M1 test shims)."""

    def get(app: ArchieApp):
        return getattr(app.session, name)

    def set_(app: ArchieApp, value) -> None:
        setattr(app.session, name, value)

    return property(get, set_, doc=doc)


class ArchieApp(App):
    """TUI client that attaches to a running agent session."""

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

    def __init__(
        self,
        ws_url: str,
        api_url: str,
        container_name: str,
        *,
        client: OrchestratorClient | None = None,
    ) -> None:
        """Attach to the session whose record is at ``api_url``.

        Args:
            ws_url: The session's stream URL.
            api_url: The session's record URL (``<base>/api/sessions/<id>``).
            container_name: The session's container, for ``!`` commands.
            client: Orchestrator access to use instead of ``ws_url``/``api_url``
                (tests); the session id still comes from ``api_url``.
        """
        super().__init__()

        self.register_theme(theme.THEME)
        self.theme = "archie"

        self._ws_url = ws_url
        self._api_url = api_url
        self._container_name = container_name
        base_url, session_id = _split_session_url(api_url)
        attached = _AttachedSession(base_url, ws_url) if client is None else None
        self._client: OrchestratorClient = attached or client
        self.session = SessionController(self._client, session_id, container_name)
        if attached is not None:
            # One WSClient for the app's lifetime, as before: commands sent while it
            # is disconnected fail inside it.
            self.session.ws = attached.ws
        self.session.attach(self)

        self._shutting_down = False
        self._quit_prompt_open = False
        self._last_esc_time = 0.0
        self._turns_ended = 0

    # --- Forwarding shims (M1; removed with this app in M2) -------------------------

    _turn_active = _forward("turn_active", "A root turn is running.")
    _reconnecting = _forward("reconnecting", "A reconnect attempt is running.")
    _reconnect_task = _forward("_reconnect_task", "The running reconnect task.")
    _receive_task = _forward("_receive_task", "The stream's receive task.")
    _editor_active = _forward("quiet", "The external editor is open (connection notices off).")
    _session_model_key = _forward("model_key", "The session's model key (model palette).")

    @property
    def _ws(self) -> WSClient | None:
        """The session's stream client."""
        return self.session.ws

    @property
    def transcript(self):
        """The session's transcript model (subagent palette, tests)."""
        return self.session.builder

    @property
    def _cumulative_cost(self) -> float:
        return self.session.ledger.cost

    def _apply_event(self, event) -> None:
        """Apply one live or history event."""
        self.session.apply(event)

    async def _replay_events(self) -> bool:
        """Replay history after the cursor; False when it could not be read."""
        return await self.session.replay()

    async def _reset_replay_state(self) -> None:
        await self.session.reset()

    async def _ensure_connected(self) -> bool:
        return await self.session.ensure_connected()

    def _estimate_context_pct(self, context_tokens: int, model_key: str) -> float:
        return estimate_context_pct(context_tokens, model_key)

    def _show_client_error(self, message: str) -> None:
        """Show a client/transport error (not recorded in the session log)."""
        self.session.client_error(message)

    # --- Layout ---------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the main UI layout."""
        yield Conversation(id="conversation")
        yield ThrobberContainer(id="throbber-container")
        yield QuestionPanel(id="question")
        yield StatusBar(id="status")
        yield MessageInput(id="input")
        yield Footer()

    async def on_mount(self) -> None:
        """Focus the input, then connect the session and replay its history."""
        self.query_one("#input", MessageInput).focus()
        await self.session.start()

    def _widget(self, selector: str, expect_type: type):
        """The widget at ``selector``, or None before the app is mounted."""
        try:
            return self.query_one(selector, expect_type)
        except Exception:  # noqa: BLE001 — no DOM yet (headless tests, early events)
            return None

    def _conversation(self) -> Conversation | None:
        """The main transcript view, or None before the app is mounted."""
        return self._widget("#conversation", Conversation)

    # --- SessionListener ------------------------------------------------------------

    def on_changes(self, changes: list[Change]) -> None:
        """Send model changes to the main view and every open popup."""
        builder = self.session.builder
        view = self._conversation()
        if view is not None:
            view.apply_changes(builder, changes)
        for screen in self._detail_screens():
            screen.node_changed(builder, changes)

    def on_local(self, entry: LocalEntry) -> None:
        """Append a client-only row."""
        conv = self._conversation()
        if conv is None:
            return
        if isinstance(entry, TurnStatusEntry):
            conv.add_turn_status(
                entry.duration_s,
                entry.input_tokens,
                entry.cache_read,
                entry.cache_write,
                entry.output_tokens,
                entry.cost,
                entry.credits,
            )
        elif isinstance(entry, ClientErrorEntry):
            conv.add_client_error(entry.message)
        else:
            conv.add_shell_output(entry.command, entry.output, exit_code=entry.exit_code)

    def on_status(self) -> None:
        """Refresh the status bar."""
        self._update_accounting_status()

    def _update_accounting_status(self) -> None:
        """Push the controller's status values to the status bar."""
        status = self._widget("#status", StatusBar)
        if status is None:
            return
        session = self.session
        if session.model_name is not None:
            status.model_name = session.model_name
        if session.supports_cache is not None:
            status.supports_cache = session.supports_cache
        if session.git_branch is not None:
            status.git_branch = session.git_branch
        if session.agent_session_id:
            status.session_id = session.agent_session_id
        ledger = session.ledger
        status.session_input = ledger.input_tokens
        status.session_output = session.session_output
        status.cache_read = ledger.cache_read
        status.cache_write = ledger.cache_write
        status.context_tokens = ledger.context_tokens
        status.context_pct = ledger.context_pct
        status.pricing_label = format_cost(ledger.cost, ledger.credits)

    def on_turn(self) -> None:
        """Show the throbber and input state; refocus the input after a turn ends."""
        session = self.session
        throbber = self._widget("#throbber", Throbber)
        if throbber is not None:
            throbber.display = session.thinking
        inp = self._widget("#input", MessageInput)
        if inp is not None:
            inp.disabled = session.input_locked
        if session.turns_ended != self._turns_ended:
            self._turns_ended = session.turns_ended
            conv = self._conversation()
            if conv is not None:
                conv.finish_streams()
            if inp is not None:
                inp.focus()

    def on_question(self) -> None:
        """Show or hide the question panel and route selection keys to it."""
        session = self.session
        panel = self._widget("#question", QuestionPanel)
        inp = self._widget("#input", MessageInput)
        if session.question is not None:
            if panel is not None:
                panel.show(session.question)
            throbber = self._widget("#throbber", Throbber)
            if throbber is not None:
                throbber.display = session.thinking
            if inp is not None:
                inp.choice_handler = self._on_choice_key
                inp.disabled = session.input_locked
                inp.focus()
        else:
            if panel is not None:
                panel.hide()
            if inp is not None:
                inp.choice_handler = None
                inp.disabled = session.input_locked

    def on_loaded(self) -> None:
        """The first history replay landed; nothing extra to show in this app."""

    async def on_reset(self) -> None:
        """Close popups of the old model and drop every transcript row."""
        while isinstance(self._top_screen(), DetailScreen):
            await self.pop_screen()
        conv = self._conversation()
        if conv is not None:
            removal = conv.reset()
            if hasattr(removal, "__await__"):
                await removal

    def on_replay(self, active: bool) -> None:
        """Mount rows once per history batch, from final node state."""
        conv = self._conversation()
        if conv is None:
            return
        if active:
            conv.suspend()
        else:
            conv.resume()

    def on_notice(self, message: str, timeout: float | None) -> None:
        """Show a transient connection notice."""
        if timeout is None:
            self.notify(message)
        else:
            self.notify(message, timeout=timeout)

    # --- Popups ---------------------------------------------------------------------

    def _top_screen(self):
        try:
            return self.screen
        except Exception:  # noqa: BLE001 — headless tests have no screen stack
            return None

    def _detail_screens(self) -> list[DetailScreen]:
        try:
            stack = self.screen_stack
        except Exception:  # noqa: BLE001 — headless tests have no screen stack
            return []
        return [screen for screen in stack if isinstance(screen, DetailScreen)]

    def _crumbs(self) -> list[str]:
        """Breadcrumb levels for a popup opened from the current screen."""
        top = self._top_screen()
        if isinstance(top, DetailScreen):
            return list(top.crumbs)
        return [self.session.agent_session_id or "Session"]

    def on_tool_clicked(self, message: ToolClicked) -> None:
        self.push_screen(ToolDetailScreen(message.tool, self._crumbs()))

    def on_subagent_clicked(self, message: SubagentClicked) -> None:
        self.push_screen(SubagentScreen(message.sub, self._crumbs()))

    def open_child_detail(self, key: tuple[str, int]) -> None:
        """Open the transcript popup for a known child (command palette)."""
        child = self.session.builder.subagents.get(key)
        if child is None:
            return
        self.push_screen(SubagentScreen(child, self._crumbs()))

    @property
    def _active_child_key(self) -> tuple[str, int] | None:
        """The subagent ``ctrl+x`` stops: the topmost open subagent popup's."""
        for screen in reversed(self._detail_screens()):
            if isinstance(screen, SubagentScreen):
                return (screen.sub.scope, screen.sub.index)
        return None

    async def action_subagent_picker(self) -> None:
        """Open the command palette filtered to subagent entries."""
        self.action_command_palette()

    def action_stop_child(self) -> None:
        """Target-stop the selected child while leaving siblings active."""
        key = self._active_child_key
        if key is not None:
            self.session.stop_child(*key)

    # --- Input ----------------------------------------------------------------------

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Hand submitted input to the session."""
        self.session.submit(event.content)

    def _on_choice_key(self, key: str) -> bool:
        """Selection keys on an empty input while a question is shown."""
        question = self.session.question
        if question is None:
            return False
        panel = self.query_one("#question", QuestionPanel)
        if key in ("up", "down"):
            panel.move(-1 if key == "up" else 1)
            return True
        if key in ("enter", "kp_enter"):
            self.session.answer(AnswerCommand(question_id=question.id, option=panel.selected))
            return True
        if key.isdigit() and 1 <= int(key) <= len(question.options):
            self.session.answer(AnswerCommand(question_id=question.id, option=int(key) - 1))
            return True
        return False

    # --- Actions --------------------------------------------------------------------

    def action_copy_block(self) -> None:
        """Copy the current text selection, or the focused message block."""
        selected = self.screen.get_selected_text()
        if selected is not None:
            self.copy_to_clipboard(selected)
            self.notify("Copied to clipboard")
            return

        focused = self.focused
        if focused is None:
            return

        for widget in focused.ancestors_with_self:
            get_copy_text = getattr(widget, "get_copy_text", None)
            if get_copy_text is None:
                continue
            text = get_copy_text()
            if text:
                self.copy_to_clipboard(text)
                self.notify("Copied to clipboard")
                return

    def action_cancel(self) -> None:
        """Esc: kill a running shell command, dismiss the question, or interrupt.

        When idle, a double tap within 500 ms clears the input.
        """
        action = self.session.cancel()
        if action == "shell":
            return
        if action is not None:
            self._last_esc_time = 0
            return
        now = time.monotonic()
        inp = self.query_one("#input", MessageInput)
        if self._last_esc_time and (now - self._last_esc_time) < 0.5 and inp.text:
            inp.clear()
            self._last_esc_time = 0
        else:
            self._last_esc_time = now

    async def action_quit(self) -> None:
        """Prompt before leaving, optionally terminating the attached session."""
        if self._shutting_down or self._quit_prompt_open:
            return
        self._quit_prompt_open = True
        self.push_screen(QuitScreen(), self._finish_quit)

    async def _finish_quit(self, terminate_session: bool | None) -> None:
        """Complete quit after the confirmation screen has been answered."""
        self._quit_prompt_open = False
        if terminate_session is None:
            return
        self._shutting_down = True
        await self.session.close()

        if terminate_session:
            try:
                status_code = await self._client.delete(self.session.session_id)
                if status_code not in {200, 404, 409}:
                    self._show_client_error(f"Failed to terminate session: {status_code}")
            except OrchestratorError as exc:
                self._show_client_error(f"Failed to terminate session: {exc}")
        await super().action_quit()

    async def _recover_after_editor(self) -> None:
        """Restore the stream after returning from the blocking editor."""
        try:
            if not await self.session.force_reconnect():
                self._show_client_error("Unable to reconnect after closing the editor.")
        except Exception as exc:  # noqa: BLE001 — surface recovery failures only
            self._show_client_error(f"Unable to reconnect after closing the editor: {exc}")
        finally:
            self._editor_active = False

    def action_editor(self) -> None:
        """Open $EDITOR for message composition. Auto-submits on save."""
        if self.session.turn_active:
            return
        self._editor_active = True

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
            asyncio.create_task(self._recover_after_editor())

    def switch_model(self, model_key: str) -> None:
        """Send a model switch command to the agent (called by ModelProvider)."""
        if not self.session.switch_model(model_key):
            self.notify("Cannot switch model during active turn", severity="warning")
