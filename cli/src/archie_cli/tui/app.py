"""The multi-session TUI: a sidebar of live sessions beside the on-screen session's pane.

``ArchieApp`` talks to one orchestrator through an ``OrchestratorClient`` (084 D19). It
follows the sessions feed, which alone drives the sidebar (D8), and keeps a
``SessionController`` for each session visited, up to ``MAX_CONTROLLERS`` (D7). Only the
session on screen has a ``SessionPane``; switching detaches the old pane from its
controller, removes it, and mounts a new pane rendered from the target's controller.
Switches are serialised by a lock and numbered, so a switch overtaken by a newer one
does nothing (D10).

Threading model: Textual runs an asyncio event loop on the main thread; the feed and
the controllers' receive loops run as tasks on the same loop, so their notifications
update widgets directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import webbrowser
from collections import OrderedDict

from archie_shared.session import SessionDescriptor, container_name
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static
from textual.worker import Worker

from archie_cli.tui import theme
from archie_cli.tui.client import FeedEvent, OrchestratorClient, ProfileInfo
from archie_cli.tui.detail import DetailScreen
from archie_cli.tui.input import MessageInput
from archie_cli.tui.models_provider import ModelProvider
from archie_cli.tui.pane import SessionPane
from archie_cli.tui.session import SessionController
from archie_cli.tui.sidebar import SessionList, Sidebar
from archie_cli.tui.transcript_view import SubagentClicked, ToolClicked

log = logging.getLogger(__name__)

MAX_CONTROLLERS = 5
"""Controllers of visited sessions kept connected (084 D7)."""

FEED_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
"""Seconds between sessions-feed reconnects; the last repeats."""

ANIMATION_INTERVAL = 1 / 8
"""Seconds between animation frames (084 D16)."""

AGE_INTERVAL = 30.0
"""Seconds between redraws of the sidebar's ages."""

EMPTY_TEXT = "No sessions — ctrl+n to start one"
CONNECTING_TEXT = "Connecting…"

_SWITCH_ACTIONS = frozenset({"step_session", "jump_session"})


class Body(Horizontal):
    """The window: sidebar and main area side by side."""

    DEFAULT_CSS = """
    Body {
        height: 1fr;
    }
    """


class Main(Vertical):
    """The area right of the sidebar: one ``SessionPane`` or an ``EmptyPane``."""

    DEFAULT_CSS = """
    Main {
        width: 1fr;
        height: 1fr;
    }
    """


class EmptyPane(Static):
    """Shown in place of a session pane: before the feed's snapshot, or with no sessions."""

    DEFAULT_CSS = """
    EmptyPane {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    """


class ArchieApp(App):
    """TUI client for every session of one orchestrator.

    Attributes:
        profile: The orchestrator profile this window talks to.
        records: Session records from the feed, by session ID.
        current: ID of the session on screen, or None.
        sidebar: The sidebar widget.
    """

    TITLE = "Archie"
    CSS_PATH = "archie.tcss"
    COMMANDS = App.COMMANDS | {ModelProvider}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "copy_block", "Copy Block", show=False),
        Binding("ctrl+g", "editor", "Editor", show=False),
        Binding("ctrl+x", "stop_child", "Stop selected child", show=False),
        Binding("alt+up", "step_session(-1)", "Previous session", show=False, priority=True),
        Binding("alt+down", "step_session(1)", "Next session", show=False, priority=True),
        *(
            Binding(f"alt+{n}", f"jump_session({n})", show=False, priority=True)
            for n in range(1, 10)
        ),
        Binding("tab", "cycle_focus(1)", show=False, priority=True),
        Binding("shift+tab", "cycle_focus(-1)", show=False, priority=True),
    ]

    def __init__(
        self, client: OrchestratorClient, *, profile: ProfileInfo, focus: str | None = None
    ) -> None:
        """Create the window.

        Args:
            client: Access to the orchestrator.
            profile: The orchestrator's profile (sidebar label, web link, local rule).
            focus: Session to show first; None shows the newest live session.
        """
        super().__init__()
        self.register_theme(theme.THEME)
        self.theme = "archie"

        self._client = client
        self.profile = profile
        self._focus = focus
        self.records: dict[str, SessionDescriptor] = {}
        self.current: str | None = None
        self.sidebar = Sidebar(profile)
        self._main = Main()
        self._placeholder: EmptyPane | None = EmptyPane(CONNECTING_TEXT)
        self._pane: SessionPane | None = None
        self._controllers: OrderedDict[str, SessionController] = OrderedDict()
        self._start_tasks: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._switch_lock = asyncio.Lock()
        self._generation = 0
        self._snapshot_seen = False
        self._shutting_down = False
        self._feed_worker: Worker | None = None
        self._animation = None
        self._frame = 0

    # --- Layout -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """The sidebar and the main area."""
        with Body():
            yield self.sidebar
            with self._main:
                assert self._placeholder is not None
                yield self._placeholder

    def on_mount(self) -> None:
        """Start the animation and age timers and follow the sessions feed."""
        self._animation = self.set_interval(ANIMATION_INTERVAL, self._tick, pause=True)
        self.set_interval(AGE_INTERVAL, self.sidebar.sessions.refresh_rows)
        self._feed_worker = self.run_worker(self._follow_feed(), group="feed", exit_on_error=False)

    async def on_unmount(self) -> None:
        """Stop the feed and close every controller."""
        await self._close_sessions()

    @property
    def pane(self) -> SessionPane | None:
        """The mounted session pane, or None."""
        return self._pane

    @property
    def controller(self) -> SessionController | None:
        """The on-screen session's controller, or None."""
        return self._controllers.get(self.current) if self.current is not None else None

    @property
    def controllers(self) -> dict[str, SessionController]:
        """Controllers kept, least recently shown first."""
        return dict(self._controllers)

    # --- Feed -------------------------------------------------------------------------

    async def _follow_feed(self) -> None:
        """Follow the sessions feed, reconnecting with back-off when it drops."""
        attempt = 0
        while not self._shutting_down:
            try:
                async for event in self._client.feed():
                    attempt = 0
                    await self._on_feed(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure means reconnect
                log.warning("Sessions feed lost: %s", exc)
            if self._shutting_down:
                return
            delay = FEED_RETRY_DELAYS[min(attempt, len(FEED_RETRY_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _on_feed(self, event: FeedEvent) -> None:
        if event[0] == "snapshot":
            await self._on_snapshot(event[1])
        else:
            self._on_record(event[1])

    async def _on_snapshot(self, sessions: list[SessionDescriptor]) -> None:
        """Replace every record; on the first snapshot, show the initial session."""
        records = {record.session_id: record for record in sessions}
        current = self.current
        if current is not None and current not in records and current in self.records:
            records[current] = self.records[current]  # it ended meanwhile; keep showing it
        self.records = records
        self._records_changed()
        if not self._snapshot_seen:
            self._snapshot_seen = True
            target = self._initial_session()
            if target is None:
                if self._placeholder is not None:
                    self._placeholder.update(EMPTY_TEXT)
            else:
                await self.switch_to(target)

    def _on_record(self, record: SessionDescriptor) -> None:
        """Update one session's row (and the pane when it is on screen)."""
        self.records[record.session_id] = record
        self._records_changed()

    def _records_changed(self) -> None:
        self.sidebar.show(self.records, self.current)
        pane = self._pane
        if pane is not None and self.current in self.records:
            pane.update_record(self.records[self.current])
        for session_id, controller in self._controllers.items():
            self._maybe_start(session_id, controller)
        self._update_animation()

    def _initial_session(self) -> str | None:
        """``focus`` when the feed knows it, else the newest live session."""
        if self._focus is not None and self._focus in self.records:
            return self._focus
        live = [record for record in self.records.values() if not record.ended]
        if not live:
            return None
        return max(live, key=lambda record: (record.started_at, record.session_id)).session_id

    # --- Controllers ------------------------------------------------------------------

    def _controller_for(self, record: SessionDescriptor) -> SessionController:
        """The session's controller, created on first visit, marked most recent."""
        session_id = record.session_id
        controller = self._controllers.get(session_id)
        if controller is None:
            name = record.container_name or container_name(session_id)
            controller = SessionController(self._client, session_id, name)
            self._controllers[session_id] = controller
        else:
            self._controllers.move_to_end(session_id)
        return controller

    def _maybe_start(self, session_id: str, controller: SessionController) -> None:
        """Start a controller once its record is known and has left ``starting``."""
        record = self.records.get(session_id)
        if controller.started or record is None or record.status == "starting":
            return
        if self._shutting_down:
            return
        controller.started = True  # ``start`` runs in the background
        task = asyncio.create_task(controller.start(live=not record.ended))
        self._start_tasks[session_id] = task

        def done(finished: asyncio.Task) -> None:
            if self._start_tasks.get(session_id) is finished:
                del self._start_tasks[session_id]

        task.add_done_callback(done)

    def _evict(self) -> None:
        """Close the least recently shown evictable controllers beyond the limit (D7)."""
        while len(self._controllers) > MAX_CONTROLLERS:
            victim = next(
                (
                    session_id
                    for session_id, controller in self._controllers.items()
                    if session_id != self.current and controller.evictable
                ),
                None,
            )
            if victim is None:
                return
            controller = self._controllers.pop(victim)
            self._spawn(self._close_controller(victim, controller))

    async def _close_controller(self, session_id: str, controller: SessionController) -> None:
        task = self._start_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — closing must continue
                log.debug("Session %s start ended during close: %s", session_id, exc)
        controller.detach()
        await controller.close()

    async def _close_sessions(self) -> None:
        """Stop following the feed and close every controller (sessions keep running)."""
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._feed_worker is not None:
            self._feed_worker.cancel()
        if self._pane is not None:
            self._pane.detach()
        controllers = list(self._controllers.items())
        self._controllers.clear()
        await asyncio.gather(
            *(self._close_controller(sid, c) for sid, c in controllers),
            return_exceptions=True,
        )

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- Switching --------------------------------------------------------------------

    async def switch_to(self, session_id: str) -> None:
        """Show ``session_id`` in the pane (084 D10).

        Switches run one at a time; one that a newer switch overtook while it waited
        for its turn does nothing.
        """
        self._generation += 1
        generation = self._generation
        async with self._switch_lock:
            if generation != self._generation or self._shutting_down:
                return
            await self._show(session_id)

    async def _show(self, session_id: str) -> None:
        record = self.records.get(session_id)
        if record is None or (session_id == self.current and self._pane is not None):
            return
        old = self._pane
        if old is not None:
            await old.close_popups()
        # No frame may be drawn from the old pane's removal until the new pane is in.
        # Textual prunes a removed widget's descendants one by one but drops them from
        # the screen's layout only after the whole subtree is gone, so a repaint in
        # between (moving focus off the input requests one) draws pruned widgets: a
        # pruned TextArea (the input) has lost its component styles and raises
        # KeyError 'text-area--gutter'. The layout refresh makes the next frame drop
        # the removed widgets before it draws anything.
        with self.batch_update():
            if old is not None:
                old.detach()
                self._pane = None
                await old.remove()
            if self._placeholder is not None:
                await self._placeholder.remove()
                self._placeholder = None

            self.current = session_id
            controller = self._controller_for(record)
            pane = SessionPane(record)
            await self._main.mount(pane)
            pane.attach(controller)
            self._pane = pane
            self._maybe_start(session_id, controller)
            self._evict()
            self._records_changed()
            pane.focus_input()
            self._main.screen.refresh(layout=True)

    def _request_switch(self, session_id: str) -> None:
        self.run_worker(self.switch_to(session_id), group="switch", exit_on_error=False)

    def on_session_list_chosen(self, message: SessionList.Chosen) -> None:
        """A sidebar row was clicked."""
        message.stop()
        self._request_switch(message.session_id)

    def action_step_session(self, step: int) -> None:
        """alt+↑/↓: show the previous or next session in sidebar order (wrapping)."""
        order = self.sidebar.sessions.order
        if not order:
            return
        if self.current in order:
            index = (order.index(self.current) + step) % len(order)
        else:
            index = 0 if step > 0 else len(order) - 1
        self._request_switch(order[index])

    def action_jump_session(self, number: int) -> None:
        """alt+1…9: show the n-th session in sidebar order."""
        order = self.sidebar.sessions.order
        if 1 <= number <= len(order):
            self._request_switch(order[number - 1])

    def action_cycle_focus(self, step: int) -> None:
        """tab/shift+tab: move focus between the session list, transcript and input."""
        targets = [self.sidebar.sessions]
        if self._pane is not None:
            targets.append(self._pane.conversation)
            if not self._pane.input.disabled:
                targets.append(self._pane.input)
        focused = self.focused
        chain = list(focused.ancestors_with_self) if focused is not None else []
        index = next((i for i, target in enumerate(targets) if target in chain), -1)
        targets[(index + step) % len(targets)].focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Session switching and focus keys act only on the main screen (or a popup)."""
        if action in _SWITCH_ACTIONS or action == "cycle_focus":
            try:
                screen = self.screen
                main = self.screen_stack[0]
            except Exception:  # noqa: BLE001 — no screen yet
                return False
            if screen is main:
                return True
            return action in _SWITCH_ACTIONS and isinstance(screen, DetailScreen)
        return True

    # --- Animation --------------------------------------------------------------------

    def _tick(self) -> None:
        self._frame += 1
        animated = self.sidebar.sessions.tick(self._frame)
        if self._pane is not None and self._pane.tick(self._frame):
            animated = True
        if not animated and self._animation is not None:
            self._animation.pause()

    def _update_animation(self) -> None:
        if self._animation is None:
            return
        pane_animates = self._pane is not None and self._pane.animated
        if self.sidebar.sessions.animated or pane_animates:
            self._animation.resume()

    # --- Popups (rows inside popups) --------------------------------------------------

    def on_tool_clicked(self, message: ToolClicked) -> None:
        """A tool row inside a popup: open its popup from the pane."""
        if self._pane is not None:
            self._pane.open_tool(message.tool)

    def on_subagent_clicked(self, message: SubagentClicked) -> None:
        """A subagent row inside a popup: open its popup from the pane."""
        if self._pane is not None:
            self._pane.open_subagent(message.sub)

    @property
    def active_child_key(self) -> tuple[str, int] | None:
        """The subagent ``ctrl+x`` stops, or None."""
        return self._pane.active_child_key if self._pane is not None else None

    def action_stop_child(self) -> None:
        """Target-stop the selected child while leaving siblings active."""
        if self._pane is not None:
            self._pane.stop_child()

    # --- Actions ----------------------------------------------------------------------

    def action_cancel(self) -> None:
        """Esc: forwarded to the on-screen session."""
        if self._pane is not None:
            self._pane.cancel()

    def action_open_web(self) -> None:
        """Open the orchestrator's web interface in the browser (084 D34)."""
        webbrowser.open(self.profile.web_url)

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

    async def action_quit(self) -> None:
        """Close the window at once; the sessions keep running (until M7's dialog)."""
        await self._close_sessions()
        self.exit()

    @property
    def session_model_key(self) -> str:
        """The on-screen session's model key (model palette)."""
        controller = self.controller
        return controller.model_key if controller is not None else ""

    def switch_model(self, model_key: str) -> None:
        """Ask the on-screen session to switch model (called by ModelProvider)."""
        controller = self.controller
        if controller is None:
            return
        if not controller.switch_model(model_key):
            self.notify("Cannot switch model during active turn", severity="warning")

    # --- External editor --------------------------------------------------------------

    async def _recover_after_editor(self, controller: SessionController) -> None:
        """Restore the on-screen session's stream after the blocking editor returns."""
        try:
            if controller.live and not controller.closing:
                if not await controller.force_reconnect():
                    controller.client_error("Unable to reconnect after closing the editor.")
        except Exception as exc:  # noqa: BLE001 — surface recovery failures only
            controller.client_error(f"Unable to reconnect after closing the editor: {exc}")
        finally:
            controller.quiet = False

    def action_editor(self) -> None:
        """Open $EDITOR for message composition. Auto-submits on save."""
        pane = self._pane
        controller = self.controller
        if pane is None or controller is None or controller.turn_active:
            return
        controller.quiet = True

        editor = os.environ.get("EDITOR", "nano")
        inp = pane.input
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
            self._spawn(self._recover_after_editor(controller))
