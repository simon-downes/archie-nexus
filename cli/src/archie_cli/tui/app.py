"""The multi-session TUI: a sidebar of live sessions beside the on-screen session's pane.

``ArchieApp`` talks to one orchestrator through an ``OrchestratorClient`` (084 D19). It
follows the sessions feed, which alone drives the sidebar (D8), and keeps a
``SessionController`` for each session visited, up to ``MAX_CONTROLLERS`` (D7). Only the
session on screen has a ``SessionPane``; switching detaches the old pane from its
controller, removes it, and mounts a new pane rendered from the target's controller.
Switches are serialised by a lock and numbered, so a switch overtaken by a newer one
does nothing (D10).

Feed rules (D9, D17, D18): the feed reconnects forever with back-off, and every
snapshot re-syncs the rows and controllers (an on-screen session missing from it is
fetched on its own). A ``session`` event for a session that is not on screen is
compared with its previous record and may post one notice in the sidebar; the
on-screen session's ``unseen`` is cleared with a PATCH. Each controller asks the app
whether its record is live, so its reconnects stop once the feed says it ended.

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

import msgspec
from archie_shared.session import SessionDescriptor, container_name
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static
from textual.worker import Worker

from archie_cli.tui import theme
from archie_cli.tui.animation import AnimationClock
from archie_cli.tui.client import FeedEvent, OrchestratorClient, OrchestratorError, ProfileInfo
from archie_cli.tui.input import MessageInput
from archie_cli.tui.models_provider import ModelProvider
from archie_cli.tui.notices import NOTICE_TIMEOUT, Notice, Severity
from archie_cli.tui.pane import SessionPane
from archie_cli.tui.session import SessionController
from archie_cli.tui.sidebar import SessionList, Sidebar

log = logging.getLogger(__name__)

MAX_CONTROLLERS = 5
"""Controllers of visited sessions kept connected (084 D7)."""

FEED_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
"""Seconds between sessions-feed reconnects; the last repeats."""

AGE_INTERVAL = 30.0
"""Seconds between redraws of the sidebar's ages."""

COPY_NOTICE_TIMEOUT = 2.0
"""Seconds the "Copied to clipboard" notice shows."""

EMPTY_TEXT = "No sessions — ctrl+n to start one"
CONNECTING_TEXT = "Connecting…"

QUESTION_CLIP = 80
"""Characters of a question shown in a "Needs an answer" notice."""

_SWITCH_ACTIONS = frozenset({"step_session", "jump_session"})


def _clip(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def transition_notice(
    old: SessionDescriptor, new: SessionDescriptor
) -> tuple[str, Severity] | None:
    """The notice text and severity for a session's record change, or None (084 D17).

    At most one per change, the most important first: the session ended ("Failed to
    start" with its error for ``failed``), it asks a question (``blocked``), a turn
    failed (``error``), or a root turn ended with a reply (``unseen`` became set).
    """
    if new.ended and not old.ended:
        if new.status == "failed":
            detail = f": {_clip(new.error, QUESTION_CLIP)}" if new.error else ""
            return f"Failed to start{detail}", "error"
        return "Session ended", "information"
    if new.status == "blocked" and old.status != "blocked":
        question = new.question.question if new.question is not None else ""
        detail = f": {_clip(question, QUESTION_CLIP)}" if question else ""
        return f"Needs an answer{detail}", "warning"
    if new.status == "error" and old.status != "error":
        return "Turn failed", "error"
    if new.unseen and not old.unseen:
        return "New reply", "information"
    return None


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
        animation: The one animation clock (084 D16); animated widgets register with it.
    """

    TITLE = "Archie"
    CSS_PATH = "archie.tcss"
    COMMANDS = App.COMMANDS | {ModelProvider}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "copy_block", "Copy Block", show=False),
        Binding("ctrl+g", "editor", "Editor", show=False),
        # Priority, so it reaches the app while the input (where ctrl+x cuts) has focus;
        # ``check_action`` passes the key on when no subagent view is open.
        Binding("ctrl+x", "stop_child", "Stop selected child", show=False, priority=True),
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
        self._fetching: set[str] = set()  # sessions being fetched (missing from a snapshot)
        self._seen_inflight: set[str] = set()  # sessions with an ``unseen`` PATCH running
        self._seen_sent: dict[str, SessionDescriptor] = {}  # the record each was sent for
        self.animation = AnimationClock()

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
        self.animation.start(self)
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
        """Replace every record and re-sync the controllers (no notices).

        The on-screen session keeps its record when the snapshot lacks it (it ended
        while the feed was down) and is fetched on its own. Controllers of live
        sessions whose stream is down reconnect; those of sessions no longer listed
        stop. On the first snapshot, show the initial session.
        """
        records = {record.session_id: record for record in sessions}
        current = self.current
        if current is not None and current not in records and current in self.records:
            records[current] = self.records[current]
            self._fetch_record(current)
        self.records = records
        self._records_changed()
        for controller in self._controllers.values():
            controller.resume()
        if not self._snapshot_seen:
            self._snapshot_seen = True
            target = self._initial_session()
            if target is None:
                if self._placeholder is not None:
                    self._placeholder.update(EMPTY_TEXT)
            else:
                await self.switch_to(target)

    def _on_record(self, record: SessionDescriptor) -> None:
        """Update one session's row (and the pane when it is on screen).

        A change to a session that is not on screen may post a notice; clicking it
        shows the session.
        """
        session_id = record.session_id
        old = self.records.get(session_id)
        self.records[session_id] = record
        if old is not None and session_id != self.current:
            change = transition_notice(old, record)
            if change is not None:
                text, severity = change
                self.notice(record.name, text, severity, session_id=session_id)
        self._records_changed()

    def _records_changed(self) -> None:
        self.sidebar.show(self.records, self.current)
        pane = self._pane
        if pane is not None and self.current in self.records:
            pane.update_record(self.records[self.current])
        for session_id, controller in self._controllers.items():
            self._maybe_start(session_id, controller)
            if controller.started and not self._record_live(session_id):
                controller.end()
        self._clear_unseen()

    def _record_live(self, session_id: str) -> bool:
        """True while the feed has a record of the session that has not ended."""
        record = self.records.get(session_id)
        return record is not None and not record.ended

    def _fetch_record(self, session_id: str) -> None:
        """Fetch a record the snapshot lacks (084 D9); one fetch per session at a time."""
        if session_id in self._fetching or self._shutting_down:
            return
        self._fetching.add(session_id)
        self._spawn(self._fetch(session_id))

    async def _fetch(self, session_id: str) -> None:
        before = self.records.get(session_id)
        try:
            try:
                record = await self._client.session(session_id)
            except OrchestratorError as exc:
                if exc.status_code != 404 or before is None:
                    log.warning("Could not fetch session %s: %s", session_id, exc)
                    return
                # The orchestrator no longer knows it: show it as ended.
                record = (
                    before if before.ended else msgspec.structs.replace(before, status="exited")
                )
            if self._shutting_down or self.records.get(session_id) is not before:
                return  # the feed sent something newer meanwhile
            self.records[session_id] = record
            self._records_changed()
        except Exception as exc:  # noqa: BLE001 — a failed fetch only waits for the next snapshot
            log.warning("Could not fetch session %s: %s", session_id, exc)
        finally:
            self._fetching.discard(session_id)

    def _clear_unseen(self) -> None:
        """PATCH ``unseen: false`` for the on-screen session when its record has it (D18).

        Sent once per record from the feed; records that arrive while a PATCH for the
        session runs count as covered by it. Failures are logged, not shown.
        """
        session_id = self.current
        record = self.records.get(session_id) if session_id is not None else None
        if session_id is None or record is None or not record.unseen or self._shutting_down:
            return
        if self._seen_sent.get(session_id) is record:
            return
        if session_id in self._seen_inflight:
            # Sent before the running PATCH was answered: that PATCH covers it.
            self._seen_sent[session_id] = record
            return
        self._seen_inflight.add(session_id)
        self._seen_sent[session_id] = record
        self._spawn(self._mark_seen(session_id))

    async def _mark_seen(self, session_id: str) -> None:
        try:
            await self._client.patch(session_id, {"unseen": False})
        except Exception as exc:  # noqa: BLE001 — logged, not shown (D18)
            log.warning("Could not mark session %s seen: %s", session_id, exc)
        finally:
            self._seen_inflight.discard(session_id)

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
            controller = SessionController(
                self._client, session_id, name, is_live=lambda: self._record_live(session_id)
            )
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

    def on_notice_chosen(self, message: Notice.Chosen) -> None:
        """A notice about a session was clicked: show that session."""
        message.stop()
        self._request_switch(message.session_id)

    # --- Notices ----------------------------------------------------------------------

    def notice(
        self,
        title: str,
        text: str = "",
        severity: Severity = "information",
        *,
        timeout: float = NOTICE_TIMEOUT,
        session_id: str | None = None,
    ) -> None:
        """Show a notice in the sidebar (084 D17).

        Args:
            title: The bold first line.
            text: The text under it.
            severity: The colour of its bar.
            timeout: Seconds until it goes.
            session_id: The session it is about; clicking it shows that session.
        """
        self.sidebar.notices.add(title, text, severity, timeout=timeout, session_id=session_id)

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
            targets.append(self._pane.transcript)  # the open detail view's, if any
            if not self._pane.input.disabled:
                targets.append(self._pane.input)
        focused = self.focused
        chain = list(focused.ancestors_with_self) if focused is not None else []
        index = next((i for i, target in enumerate(targets) if target in chain), -1)
        targets[(index + step) % len(targets)].focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Switching and focus keys act only on the main screen; ``ctrl+x`` only while a
        subagent view is open (otherwise the key goes on to the focused widget)."""
        if action in _SWITCH_ACTIONS or action == "cycle_focus":
            try:
                return self.screen is self.screen_stack[0]
            except Exception:  # noqa: BLE001 — no screen yet
                return False
        if action == "stop_child":
            return self.active_child_key is not None
        return True

    # --- Subagent views ---------------------------------------------------------------

    @property
    def active_child_key(self) -> tuple[str, int] | None:
        """The subagent ``ctrl+x`` stops (the pane's topmost subagent view's), or None."""
        return self._pane.active_child_key if self._pane is not None else None

    def action_stop_child(self) -> None:
        """ctrl+x: stop the child shown in the open subagent view; siblings keep running."""
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
            self.notice("Copied to clipboard", timeout=COPY_NOTICE_TIMEOUT)
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
                self.notice("Copied to clipboard", timeout=COPY_NOTICE_TIMEOUT)
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
            self.notice(
                "Model not switched", "Cannot switch model during an active turn", "warning"
            )

    # --- External editor --------------------------------------------------------------

    async def _recover_after_editor(self, controller: SessionController) -> None:
        """Restore the on-screen session's stream after the blocking editor returns."""
        try:
            if controller.live and not controller.closing:
                # A reconnect still running afterwards reports its outage itself.
                await controller.force_reconnect()
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
                self.notice("Editor", editor_error, "error")
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
