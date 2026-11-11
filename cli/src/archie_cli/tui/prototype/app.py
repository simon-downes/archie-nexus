"""The prototype app: a fixed sessions sidebar beside one session's pane.

Layout::

    ┌ sidebar ──────────────────┬ session pane ──────────────────────────────────────┐
    │ brand, filter              │ header: state, name, badge; status bar       │
    │ workspace groups           │ transcript, or a tool/subagent view on top   │
    │                            │ activity line, question                      │
    │ f1 Keys │ ^q Quit │ ^p …    │ input                                        │
    └────────────────────────────┴───────────────────────────────────────────────┘

Transcripts, tool rows, the question panel, the status bar and the input are the
attach TUI's widgets. Live behaviour is mocked: sending a message streams a canned
reply, Esc interrupts it, and answering a question resumes the session.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from archie_shared import __version__
from archie_shared.events import PendingQuestion
from archie_shared.transcript import TranscriptBuilder
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Input, Static
from textual.worker import Worker

from archie_cli.tui import theme
from archie_cli.tui.conversation import (
    CancelledMessage,
    ErrorMessage,
    StreamingMessage,
    UserMessage,
    format_cost,
)
from archie_cli.tui.input import MessageInput
from archie_cli.tui.prototype import data, rows
from archie_cli.tui.prototype.commands import MODEL_PLACEHOLDER, ModelCommands, SessionCommands
from archie_cli.tui.prototype.data import MockSession
from archie_cli.tui.prototype.dialogs import (
    ConfirmDialog,
    KeysDialog,
    NewSession,
    NewSessionDialog,
    RenameDialog,
)
from archie_cli.tui.prototype.panes import (
    PANE_BG,
    PaneView,
    SubagentView,
    ToolDetailView,
    subagent_crumb,
    tool_crumb,
)
from archie_cli.tui.prototype.widgets import (
    ActivityLine,
    NoticeStack,
    SessionHeader,
    SessionList,
    SessionStatusBar,
    key_hints,
    model_label,
    state_label,
)
from archie_cli.tui.question import QuestionPanel
from archie_cli.tui.status import StatusBar
from archie_cli.tui.transcript_view import Conversation, SubagentClicked, ToolClicked

_TUI_DIR = Path(__file__).resolve().parents[1]

MOCK_REPLY = (
    "This is the **prototype**, so no agent is attached and this reply is canned.\n\n"
    "In the real client the message goes to this session's agent over its WebSocket, "
    "and only the visible session keeps a live connection. The other sessions update "
    "from the sessions feed, which drives the sidebar states."
)

# Seconds after start-up when a background working session finishes (shows a
# sidebar state change and a toast for a session you aren't looking at).
BACKGROUND_FINISH_AFTER = 20.0


def _view_id(session_id: str) -> str:
    return f"session-{session_id}"


class PrototypeApp(App):
    """Multi-session TUI mock-up over historical session logs."""

    TITLE = "Archie"
    CSS_PATH = [_TUI_DIR / "archie.tcss", _TUI_DIR / "prototype" / "prototype.tcss"]
    COMMANDS = {SessionCommands}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_session", "New session"),
        Binding("f1", "keys", "Keys"),
        Binding("alt+m", "change_model", "Change model"),
        Binding("escape", "cancel", "Back / interrupt"),
        Binding("ctrl+c", "copy_selection", "Copy"),
        Binding("alt+up", "step_session(-1)", "Previous session"),
        Binding("alt+down", "step_session(1)", "Next session"),
        *(Binding(f"alt+{n}", f"jump_session({n})", show=False) for n in range(1, 10)),
    ]

    def __init__(self, sessions: list[MockSession]) -> None:
        super().__init__()
        self.register_theme(
            dataclasses.replace(theme.THEME, variables={**theme.THEME.variables, "pane": PANE_BG})
        )
        self.theme = "archie"
        self.sessions = sessions
        self.current: MockSession | None = None
        self._views: dict[str, Conversation] = {}
        self._stacks: dict[str, list[PaneView]] = {}
        self._loads: dict[str, asyncio.Task] = {}
        self._replies: dict[str, Worker] = {}
        self._frame = 0
        self._new_count = 0
        self._pane_count = 0
        self._restore_rows = lambda: None

    # -- layout ----------------------------------------------------------------

    def _q(self, selector, expect_type=None):
        """Query the main screen, which stays at the bottom of the stack under dialogs."""
        screen = self.screen_stack[0]
        if expect_type is None:
            return screen.query_one(selector)
        return screen.query_one(selector, expect_type)

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static(self._brand(), id="brand")
                yield Input(placeholder="Filter sessions", id="filter")
                yield SessionList(id="session-list")
                yield NoticeStack(id="notices")
                yield Static(
                    key_hints(("f1", "Keys"), ("^q", "Quit"), ("^p", "Palette")),
                    id="sidebar-keys",
                )
            with Vertical(id="main"):
                yield SessionHeader(id="session-header")
                with ContentSwitcher(id="views"):
                    yield Static("", id="loading")
                yield ActivityLine(id="activity")
                yield QuestionPanel(id="question")
                yield MessageInput(id="input")
                yield SessionStatusBar(id="status")

    def _brand(self) -> Text:
        live = sum(1 for s in self.sessions if s.state != "ended")
        return Text.assemble(
            ("◆ Archie", f"bold {theme.PRIMARY}"),
            (f"  v{__version__}", theme.MUTED),
            "\n",
            (f"local · {live} running · prototype", theme.MUTED),
        )

    async def on_mount(self) -> None:
        self._restore_rows = rows.install()
        self._refresh_sidebar()
        self.set_interval(1 / 8, self._tick)
        self.set_interval(30, self._refresh_sidebar)
        self.set_timer(BACKGROUND_FINISH_AFTER, self._finish_background_session)
        if self.sessions:
            await self.switch_to(self._sidebar().order[0])
        self._focus_pane()

    def on_unmount(self) -> None:
        self._restore_rows()

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """Show notifications at the foot of the sidebar instead of as corner toasts.

        Messages are shown as plain text (session names may contain brackets).
        """
        try:
            stack = self._q("#notices", NoticeStack)
        except (NoMatches, IndexError):  # before mount or after shutdown
            super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)
            return
        stack.add(
            message, title, severity, self.NOTIFICATION_TIMEOUT if timeout is None else timeout
        )

    def _focus_pane(self) -> None:
        """Focus the input, or the sidebar when the session can't take a message."""
        inp = self._q("#input", MessageInput)
        if inp.disabled:
            self._sidebar().focus()
        else:
            inp.focus()

    # -- sidebar ---------------------------------------------------------------

    def _sidebar(self) -> SessionList:
        return self._q("#session-list", SessionList)

    def _refresh_sidebar(self) -> None:
        current = self.current.session_id if self.current else None
        self._sidebar().show(self.sessions, current)
        self._q("#brand", Static).update(self._brand())

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            current = self.current.session_id if self.current else None
            self._sidebar().show(self.sessions, current, event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self._sidebar().focus()

    async def on_session_list_chosen(self, message: SessionList.Chosen) -> None:
        await self.switch_to(message.session_id)
        if not self._q("#input", MessageInput).disabled:
            self._q("#input", MessageInput).focus()

    def _tick(self) -> None:
        self._frame += 1
        try:
            self._sidebar().tick()
        except NoMatches:
            return  # shutting down: the timer outlived the widgets
        if self.current is not None and self.current.state == "working":
            self._q("#session-header", SessionHeader).show(self.current, self._frame)
            self._q("#activity", ActivityLine).show(self.current.activity, self._frame)
        if self._frame % 24 == 0:  # every 3s
            self._cycle_scripted_activity()

    def _cycle_scripted_activity(self) -> None:
        """Scripted working sessions cycle thinking, responding and working, to show each.

        Sessions running a mock reply follow their own script instead.
        """
        for session in self.sessions:
            if session.state == "working" and session.session_id not in self._replies:
                cycle = ("thinking", "responding", "working")
                following = cycle[(cycle.index(session.activity) + 1) % len(cycle)]
                self._set_activity(session, following)

    def _session(self, session_id: str) -> MockSession | None:
        return next((s for s in self.sessions if s.session_id == session_id), None)

    async def action_step_session(self, delta: int) -> None:
        order = self._sidebar().order
        if not order:
            return
        if self.current is None or self.current.session_id not in order:
            target = order[0]
        else:
            target = order[(order.index(self.current.session_id) + delta) % len(order)]
        await self.switch_to(target)

    async def action_jump_session(self, number: int) -> None:
        order = self._sidebar().order
        if number <= len(order):
            await self.switch_to(order[number - 1])

    # -- switching -------------------------------------------------------------

    async def switch_to(self, session_id: str) -> None:
        """Point the session pane at another session, loading its log on first view."""
        session = self._session(session_id)
        if session is None or session is self.current:
            return
        previous = self.current
        if previous is not None and previous.state == "idle":
            previous.unseen = False  # seen once you've looked and moved on
        self.current = session
        self._refresh_sidebar()
        self._show_pane(session)
        switcher = self._q("#views", ContentSwitcher)
        if session.session_id not in self._views:
            loading = self._q("#loading", Static)
            loading.update(Text(f"Replaying {session.session_id}…", style=theme.MUTED))
            switcher.current = "loading"
            await self._ensure_loaded(session)
            if self.current is not session:
                return  # switched again while loading
            await self._build_view(session)
        switcher.current = self._top_id(session)
        self._show_pane(session)

    def _top_id(self, session: MockSession) -> str:
        """Id of what the pane shows for ``session``: its top detail view, or the transcript."""
        stack = self._stacks.get(session.session_id)
        return stack[-1].id if stack else _view_id(session.session_id)  # type: ignore[return-value]

    async def _ensure_loaded(self, session: MockSession) -> None:
        if session.loaded:
            return
        task = self._loads.get(session.session_id)
        if task is None:
            task = asyncio.ensure_future(asyncio.to_thread(data.load, session))
            self._loads[session.session_id] = task
        await task

    async def _build_view(self, session: MockSession) -> None:
        view = Conversation(id=_view_id(session.session_id))
        view.display = False
        await self._q("#views", ContentSwitcher).mount(view)
        self._views[session.session_id] = view
        builder = session.transcript or TranscriptBuilder()
        view.suspend()
        for node in builder.preamble:
            view.add_node(node)
        for turn in builder.turns:
            view.add_node(turn)
        if session.state == "error":
            view.add_local(
                lambda: ErrorMessage(
                    "Turn failed: the provider throttled the request after 3 retries "
                    "(mock error for the prototype)."
                )
            )
        view.resume()

    def _show_pane(self, session: MockSession) -> None:
        """Header, activity line, question and input for ``session``'s state."""
        self._q("#session-header", SessionHeader).show(session, self._frame)
        self._update_status(session)
        self._show_activity(session)
        panel = self._q("#question", QuestionPanel)
        inp = self._q("#input", MessageInput)
        if session.state == "blocked" and session.loaded:
            if session.question is None:
                session.question = data.mock_question(session)
            panel.show(session.question)
            inp.choice_handler = self._on_choice_key
        else:
            panel.hide()
            inp.choice_handler = None
        inp.disabled = session.state in ("working", "ended")
        inp.placeholder = {
            "working": "Archie is working",
            "blocked": "Type an answer, or pick an option above",
            "ended": "This session has ended · read-only",
        }.get(session.state, "Message Archie · shift+enter for a new line")

    def _show_activity(self, session: MockSession) -> None:
        """The activity line while ``session`` works, blank otherwise.

        The line keeps its rows either way, so the transcript above doesn't grow and
        shrink as turns start and end.
        """
        line = self._q("#activity", ActivityLine)
        if session.state == "working":
            line.show(session.activity, self._frame)
        else:
            line.clear()

    def _set_activity(self, session: MockSession, activity: str) -> None:
        session.activity = activity  # type: ignore[assignment]
        if session is self.current:
            self._show_activity(session)

    def _update_status(self, session: MockSession) -> None:
        status = self._q("#status", StatusBar)
        acct = session.accounting
        # The header title shows the id unless the session is named; don't repeat it.
        status.session_id = session.session_id if session.name else ""
        status.model_name = model_label(session.model_key)
        status.supports_cache = not session.model_key.startswith("kiro/")
        status.session_input = acct.input_tokens
        status.session_output = acct.output_tokens
        status.cache_read = acct.cache_read
        status.cache_write = acct.cache_write
        status.context_tokens = acct.context_tokens
        status.context_pct = acct.context_pct
        status.pricing_label = format_cost(acct.cost_usd, acct.credits)

    # -- detail views in the pane ------------------------------------------------

    def _crumbs(self) -> list[Text]:
        """Levels above a new detail view; the transcript itself isn't one."""
        session = self.current
        stack = self._stacks.get(session.session_id) if session else None
        return list(stack[-1].crumbs) if stack else []

    async def _push_pane(self, make) -> None:
        session = self.current
        if session is None:
            return
        self._pane_count += 1
        view: PaneView = make(self._crumbs(), f"pane-{self._pane_count}")
        view.display = False
        await self._q("#views", ContentSwitcher).mount(view)
        self._stacks.setdefault(session.session_id, []).append(view)
        self._q("#views", ContentSwitcher).current = view.id

    async def _pop_pane(self) -> bool:
        session = self.current
        stack = self._stacks.get(session.session_id) if session else None
        if not stack:
            return False
        view = stack.pop()
        self._q("#views", ContentSwitcher).current = self._top_id(session)
        await view.remove()
        self._focus_pane()
        return True

    async def on_tool_clicked(self, message: ToolClicked) -> None:
        tool = message.tool
        await self._push_pane(
            lambda crumbs, pane_id: ToolDetailView(tool, [*crumbs, tool_crumb(tool)], id=pane_id)
        )

    async def on_subagent_clicked(self, message: SubagentClicked) -> None:
        sub = message.sub
        await self._push_pane(
            lambda crumbs, pane_id: SubagentView(sub, [*crumbs, subagent_crumb(sub)], id=pane_id)
        )

    # -- mock live behaviour ---------------------------------------------------

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        session = self.current
        if session is None or session.state in ("working", "ended"):
            return
        if session.state == "blocked":
            self._resume_after_answer(session, event.content)
            return
        self._start_mock_turn(session, event.content)

    def _on_choice_key(self, key: str) -> bool:
        session = self.current
        if session is None or session.question is None:
            return False
        question: PendingQuestion = session.question
        panel = self._q("#question", QuestionPanel)
        if key in ("up", "down"):
            panel.move(-1 if key == "up" else 1)
            return True
        if key in ("enter", "kp_enter"):
            self._resume_after_answer(session, question.options[panel.selected].label)
            return True
        if key.isdigit() and 1 <= int(key) <= len(question.options):
            self._resume_after_answer(session, question.options[int(key) - 1].label)
            return True
        return False

    def _resume_after_answer(self, session: MockSession, answer: str) -> None:
        session.question = None
        self.notify(f"Answered: {answer}", timeout=2)
        self._set_state(session, "working")
        self._replies[session.session_id] = self._reply_worker(session)

    def _start_mock_turn(self, session: MockSession, content: str) -> None:
        view = self._views.get(session.session_id)
        if view is None:
            return
        view.add_local(lambda: UserMessage(content))
        view.scroll_end(animate=False)
        self._set_state(session, "working")
        self._replies[session.session_id] = self._reply_worker(session)

    def _reply_worker(self, session: MockSession) -> Worker:
        # A worker, so quitting cancels it before the widgets go away.
        return self.run_worker(self._mock_reply_guarded(session), group="replies")

    async def _mock_reply_guarded(self, session: MockSession) -> None:
        try:
            await self._mock_reply(session)
        except NoMatches:
            pass  # the app is shutting down mid-turn and its widgets are gone

    async def _mock_reply(self, session: MockSession) -> None:
        """A mock turn: thinking, a tool call (working), thinking, then a streamed reply
        (responding).

        Ends ready, and unseen if you moved to another session meanwhile.
        """
        for activity, seconds in (("thinking", 1.2), ("working", 1.8), ("thinking", 0.8)):
            self._set_activity(session, activity)
            await asyncio.sleep(seconds)
        view = self._views.get(session.session_id)
        if view is None:
            return
        holder: list[StreamingMessage] = []

        def make() -> Widget:
            holder.append(StreamingMessage(""))
            return holder[0]

        view.add_local(make)
        self._set_activity(session, "responding")
        for word in MOCK_REPLY.split(" "):
            if holder:
                holder[0].append(word + " ")
                view.scroll_if_at_bottom()
            await asyncio.sleep(0.04)
        if holder:
            holder[0].finish()
        session.accounting.cost_usd += 0.0123
        session.cost_usd = session.accounting.cost_usd
        self._finish_turn(session)

    def _finish_turn(self, session: MockSession) -> None:
        self._replies.pop(session.session_id, None)
        session.unseen = self.current is not session
        self._set_state(session, "idle")
        if session.unseen:
            self.notify(session.display_name, title="New reply", timeout=4)

    def _finish_background_session(self) -> None:
        for session in self.sessions:
            if session.state == "working" and session is not self.current:
                if session.session_id in self._replies:
                    continue
                session.unseen = True
                self._set_state(session, "idle")
                self.notify(session.display_name, title="Session ready", timeout=5)
                return

    def _set_state(self, session: MockSession, state: data.MockState) -> None:
        session.state = state
        if state == "working":
            session.activity = "thinking"  # a turn starts with the model thinking
        session.last_activity = datetime.now(UTC)
        self._refresh_sidebar()
        if session is self.current:
            self._show_pane(session)
            if state not in ("working", "ended"):
                self._q("#input", MessageInput).focus()

    async def action_cancel(self) -> None:
        """Esc: back out of a detail view, dismiss a question, or interrupt the turn."""
        session = self.current
        if session is None or await self._pop_pane():
            return
        if session.state == "blocked":
            session.question = None
            self._set_state(session, "idle")
            self.notify("Question dismissed", timeout=2)
            return
        if session.state != "working":
            return
        worker = self._replies.pop(session.session_id, None)
        if worker is not None:
            worker.cancel()
        view = self._views.get(session.session_id)
        if view is not None:
            view.finish_streams()
            view.add_local(CancelledMessage)
        self._set_state(session, "idle")

    # -- palette and dialog actions --------------------------------------------

    def action_keys(self) -> None:
        self.push_screen(KeysDialog())

    def action_change_model(self) -> None:
        """The command palette with only the session's models (alt+m)."""
        session = self.current
        if session is None or session.read_only or CommandPalette.is_open(self):
            return
        self.push_screen(CommandPalette(providers=[ModelCommands], placeholder=MODEL_PLACEHOLDER))

    def set_model(self, model_key: str) -> None:
        session = self.current
        if session is None or model_key == session.model_key:
            return
        session.model_key = model_key
        self._update_status(session)
        self.notify(f"Model → {model_label(model_key)} (mock)", timeout=3)

    def action_compact(self) -> None:
        self.notify("Compaction requested (mock: nothing happens)", timeout=3)

    def action_rename_session(self) -> None:
        session = self.current
        if session is None:
            return

        def renamed(name: str | None) -> None:
            if name is not None:
                session.name = name
                self._refresh_sidebar()
                self._q("#session-header", SessionHeader).show(session, self._frame)
                self._update_status(session)  # a named session shows its id here

        self.push_screen(RenameDialog(session.name, session.session_id), renamed)

    def action_stop_session(self) -> None:
        session = self.current
        if session is None or session.read_only:
            return
        message = Text.assemble(
            ("Stop ", ""),
            (session.display_name, "bold"),
            ("?\n\n", ""),
            (
                "Archie interrupts any running turn and removes the container. The session "
                "log is kept; it leaves the sidebar when you switch away.",
                theme.MUTED,
            ),
        )

        def confirmed(stop: bool | None) -> None:
            if stop:
                worker = self._replies.pop(session.session_id, None)
                if worker is not None:
                    worker.cancel()
                session.question = None
                self._set_state(session, "ended")
                self.notify(f"Stopped {session.session_id} (mock)", timeout=3)

        self.push_screen(
            ConfirmDialog("Stop session", message, "Stop session", destructive=True, scope="pane"),
            confirmed,
        )

    def action_new_session(self) -> None:
        workspaces = sorted({s.workspace for s in self.sessions}) or ["archie"]
        current = self.current
        workspace = current.workspace if current else workspaces[0]
        model_key = current.model_key if current else ""
        self.push_screen(NewSessionDialog(workspaces, workspace, model_key), self._create_session)

    async def _create_session(self, request: NewSession | None) -> None:
        if request is None:
            return
        self._new_count += 1
        now = datetime.now(UTC)
        first_line = request.prompt.splitlines()[0] if request.prompt else ""
        session = MockSession(
            session_id=f"{request.workspace}-mock{self._new_count:04d}",
            workspace=request.workspace,
            title=first_line or "New session",
            name=request.name,
            log_path=None,
            started_at=now,
            last_activity=now,
            cost_usd=0.0,
            subagent_count=0,
            model_key=request.model_key,
            state="idle",
            transcript=TranscriptBuilder(),
        )
        self.sessions.append(session)
        await self._build_view(session)
        if request.prompt:
            self._start_mock_turn(session, request.prompt)
        else:
            self._refresh_sidebar()
        if request.background:
            self.notify(session.display_name, title="Started in the background", timeout=3)
        else:
            await self.switch_to(session.session_id)
            self._focus_pane()

    async def action_quit(self) -> None:
        live = [s for s in self.sessions if s.state != "ended"]
        busy = [s for s in live if s.state in ("working", "blocked")]
        message = Text("Close this window?\n\n")
        detail = f"{len(live)} sessions keep running in their containers"
        if busy:
            detail += f", {len(busy)} of them mid-turn or waiting for you"
        detail += ". Run archie tui again to reattach; Stop session ends one for good."
        message.append(detail, style=theme.MUTED)

        def confirmed(close: bool | None) -> None:
            if close:
                self.exit()

        self.push_screen(ConfirmDialog("Quit Archie", message, "Quit"), confirmed)

    def action_copy_selection(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.notify("Copied to clipboard", timeout=2)


def describe(session: MockSession) -> str:
    """One line per session for ``archie tui --list``."""
    return f"{state_label(session):<16} {session.session_id:<32} {session.title[:60]}"
