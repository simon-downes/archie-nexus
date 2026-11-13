"""The session pane: one session's header, transcript, question, input and status bar.

``SessionPane`` is the view of one ``SessionController`` (084 D6). ``attach`` renders
the controller's current state (the transcript through ``Conversation.load``, status,
turn, question, and the input's draft and history), then the pane receives the
controller's changes as its ``SessionListener``. ``detach`` hands the input's draft and
history back to the controller, so the next pane for the session restores them.

Every query is scoped to the pane: the app mounts one pane at a time (D7), but nothing
here assumes that. Tool and subagent views open on a per-pane stack in the
transcript's place (084 D13): the transcript is hidden, not unmounted, so popping the
last view shows it where it was, and the pane feeds every open view its model changes.
The stack goes with the pane, so a session switched back to opens at its transcript.
"""

from __future__ import annotations

import time

from archie_shared.commands import AnswerCommand
from archie_shared.session import SessionDescriptor
from archie_shared.transcript import Change, SubagentNode, ToolNode
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.animation import Animated, spinner
from archie_cli.tui.conversation import format_cost
from archie_cli.tui.detail import Crumb, DetailView, SubagentView, ToolDetailView
from archie_cli.tui.input import MessageInput
from archie_cli.tui.question import QuestionPanel
from archie_cli.tui.session import Activity, LocalEntry, SessionController
from archie_cli.tui.sidebar import ANIMATED_STATUSES, branch_label, state_badge, state_glyph
from archie_cli.tui.status import StatusBar
from archie_cli.tui.transcript_view import (
    LAYOUT_WAITS,
    Conversation,
    SubagentClicked,
    ToolClicked,
)

DOUBLE_ESC_S = 0.5
"""Seconds within which a second Esc clears the input."""


class SessionHeader(Animated):
    """Above the transcript: state glyph and name on the left, the state badge right.

    The badge names the record's state (084 D30): ``WORKING``, ``NEEDS AN ANSWER``,
    ``TURN FAILED``, ``READY``, ``NEW REPLY``, ``ENDED`` and the lifecycle states, on the
    state's colour. Starting, working and stopping glyphs animate on the app's clock.
    """

    DEFAULT_CSS = """
    SessionHeader {
        width: 1fr;
        height: 3;
        padding: 1 2;
    }
    """

    def __init__(self, record: SessionDescriptor) -> None:
        super().__init__()
        self.record = record

    @property
    def animating(self) -> bool:
        """True while the glyph animates."""
        return self.record.status in ANIMATED_STATUSES

    def render(self) -> Text:
        width = max(10, self.size.width)
        label, colour = state_badge(self.record)
        badge = Text(f" {label} ", style=f"bold {theme.SURFACE} on {colour}")
        line = Text.assemble(state_glyph(self.record, self.frame), " ")
        name = Text(self.record.name, style="bold")
        name.truncate(max(1, width - line.cell_len - badge.cell_len - 1), overflow="ellipsis")
        line.append_text(name)
        line.pad_right(max(0, width - line.cell_len - badge.cell_len))
        line.append_text(badge)
        line.no_wrap = True
        return line


class ActivityLine(Animated):
    """``⠋ Thinking… (esc to cancel)`` under the transcript, for the whole turn.

    The word follows ``SessionController.activity``: Thinking while a provider request
    waits, Responding while a reply streams, Working while tools run. Between turns the
    line is blank but keeps its height (one line and a blank one under it), so the
    transcript never changes height. The spinner runs on the app's clock (084 D16).
    """

    DEFAULT_CSS = """
    ActivityLine {
        width: 1fr;
        height: 2;
        padding: 0 2 1 4;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.activity: Activity | None = None

    @property
    def animating(self) -> bool:
        """True while a turn runs."""
        return self.activity is not None

    def show(self, activity: Activity | None) -> None:
        """Show ``activity``'s word, or a blank line for None."""
        if activity == self.activity:
            return
        self.activity = activity
        self.refresh()
        self.sync_animation()

    def render(self) -> Text:
        if self.activity is None:
            return Text("")
        return Text.assemble(
            (spinner(self.frame), f"bold {theme.PRIMARY}"),
            " ",
            (f"{self.activity}…", "bold"),
            (" (esc to cancel)", theme.MUTED),
            no_wrap=True,
            overflow="ellipsis",
        )


class SessionPane(Vertical):
    """The on-screen session: header, transcript, activity line, question, input, status.

    Attributes:
        record: The session's latest record from the feed.
        controller: The attached controller, or None before ``attach``/after ``detach``.
    """

    DEFAULT_CSS = """
    SessionPane {
        width: 1fr;
        height: 1fr;
    }
    SessionPane > .pane-loading {
        height: auto;
        padding: 1 2;
    }
    """

    def __init__(self, record: SessionDescriptor) -> None:
        super().__init__()
        self.record = record
        self.controller: SessionController | None = None
        self.header = SessionHeader(record)
        self.loading_line = Static("", classes="pane-loading")
        self.conversation = Conversation()
        self.activity = ActivityLine()
        self.question = QuestionPanel()
        self.input = MessageInput()
        self.status = StatusBar()
        self._turns_ended = 0
        self._last_esc_time = 0.0
        self._views: list[DetailView] = []
        self._saved_scroll: tuple[float, bool] = (0.0, True)  # the transcript's, when hidden
        self._reloading = False  # a 409 reset dropped the rows; the reload rebuilds them

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.loading_line
        yield self.conversation
        yield self.activity
        yield self.question
        yield self.input
        yield self.status

    # --- Attach / detach ----------------------------------------------------------

    def attach(self, controller: SessionController) -> None:
        """Render ``controller``'s current state, then follow its changes (084 D6, D7).

        Must be called once the pane is mounted. The transcript opens at its latest
        rows; a question shows at its recommended option.
        """
        self.controller = controller
        if controller.replaying:
            self.conversation.suspend()  # the batch's end resumes it (``on_replay``)
        self.conversation.load(controller.builder, controller.locals)
        self.input.text = controller.draft
        self.input.move_cursor(self.input.document.end)
        self.input.set_sent_history(controller.history)
        self._turns_ended = controller.turns_ended
        self._show_loading()
        self._show_branch()
        self._show_session_id()
        self.on_status()
        self.on_question()
        self._show_turn()
        controller.attach(self)

    def detach(self) -> None:
        """Stop following the controller; save the input's draft and history into it."""
        controller = self.controller
        if controller is None:
            return
        controller.detach()
        controller.draft = self.input.text
        controller.history = self.input.sent_history
        self.controller = None

    def update_record(self, record: SessionDescriptor) -> None:
        """Show a newer record of the session (header, loading line, input, branch)."""
        self.record = record
        self.header.record = record
        self.header.refresh()
        self.header.sync_animation()
        self._show_branch()
        self._show_session_id()
        self._show_loading()
        self._show_turn()

    def _show_branch(self) -> None:
        """The status bar's branch, from the record's location (084 D31)."""
        self.status.git_branch = branch_label(self.record.location) or "—"

    def _show_session_id(self) -> None:
        """The status bar's session ID: only when the header shows a name instead (D31)."""
        record = self.record
        named = bool(record.name) and record.name != record.session_id
        self.status.session_id = record.session_id if named else ""

    def focus_input(self) -> None:
        """Focus the input when it takes text."""
        if not self.input.disabled:
            self.input.focus()

    def _show_loading(self) -> None:
        controller = self.controller
        record = self.record
        style = theme.MUTED
        if record.status == "starting":
            text = "Starting…"
        elif record.status == "failed":
            # It never ran: there is no history to load, only the start error (084 D3).
            text = f"Failed to start: {record.error}" if record.error else "Failed to start"
            style = theme.ERROR
        elif controller is None or not controller.loaded:
            text = "Loading…"
        else:
            text = ""
        self.loading_line.update(Text(text, style=style))
        self.loading_line.display = bool(text)

    def _show_turn(self) -> None:
        controller = self.controller
        if controller is None:
            return
        self.activity.show(controller.activity)
        self.input.disabled = controller.input_locked or self.record.ended

    # --- SessionListener ----------------------------------------------------------

    def on_changes(self, changes: list[Change]) -> None:
        """Send model changes to the transcript and every open detail view.

        After a reset the transcript skips them: the reload's end rebuilds it.
        """
        controller = self.controller
        if controller is None:
            return
        if not self._reloading:
            self.conversation.apply_changes(controller.builder, changes)
        for view in self._views:
            view.node_changed(controller.builder, changes)

    def on_local(self, entry: LocalEntry) -> None:
        """Append a client-only row (after a reset, the reload's end places it)."""
        if not self._reloading:
            self.conversation.add_entry(entry)

    def on_status(self) -> None:
        """Push the controller's status values to the status bar."""
        controller = self.controller
        if controller is None:
            return
        status = self.status
        if controller.model_name is not None:
            status.model_name = controller.model_name
        if controller.supports_cache is not None:
            status.supports_cache = controller.supports_cache
        ledger = controller.ledger
        status.session_input = ledger.input_tokens
        status.session_output = controller.session_output
        status.cache_read = ledger.cache_read
        status.cache_write = ledger.cache_write
        status.context_tokens = ledger.context_tokens
        status.context_pct = ledger.context_pct
        status.pricing_label = format_cost(ledger.cost, ledger.credits)

    def on_turn(self) -> None:
        """Show the activity line and input state; refocus the input after a turn ends."""
        controller = self.controller
        if controller is None:
            return
        self._show_turn()
        if controller.turns_ended != self._turns_ended:
            self._turns_ended = controller.turns_ended
            self.conversation.finish_streams()
            self.focus_input()

    def on_question(self) -> None:
        """Show or hide the question panel and route selection keys to it."""
        controller = self.controller
        if controller is None:
            return
        if controller.question is not None:
            self.question.show(controller.question)
            self.input.choice_handler = self._on_choice_key
            self._show_turn()
            self.focus_input()
        else:
            if self.question.question is not None:
                self.question.hide()
            self.input.choice_handler = None
            self._show_turn()

    def on_loaded(self) -> None:
        """The first history replay landed: drop the loading line."""
        self._show_loading()

    async def on_reset(self) -> None:
        """Close the old model's detail views and drop every transcript row.

        The full reload that follows is not mounted row by row: when its batch ends
        (``on_replay(False)``), the transcript is rebuilt from the controller like on
        ``attach``, so client-only rows come back after their anchors.
        """
        await self.close_views()
        await self.conversation.reset()
        self._reloading = True

    def on_replay(self, active: bool) -> None:
        """Mount rows once per history batch, from final node state."""
        if active:
            self.conversation.suspend()
            return
        controller = self.controller
        if self._reloading and controller is not None:
            self._reloading = False
            self.conversation.load(controller.builder, controller.locals)
        self.conversation.resume()

    # --- Input --------------------------------------------------------------------

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Hand submitted input to the controller."""
        event.stop()
        if self.controller is not None:
            self.controller.submit(event.content)

    def _on_choice_key(self, key: str) -> bool:
        """Selection keys on an empty input while a question is shown."""
        controller = self.controller
        question = controller.question if controller is not None else None
        if controller is None or question is None:
            return False
        if key in ("up", "down"):
            self.question.move(-1 if key == "up" else 1)
            return True
        if key in ("enter", "kp_enter"):
            controller.answer(AnswerCommand(question_id=question.id, option=self.question.selected))
            return True
        if key.isdigit() and 1 <= int(key) <= len(question.options):
            controller.answer(AnswerCommand(question_id=question.id, option=int(key) - 1))
            return True
        return False

    def cancel(self) -> None:
        """Esc, first match wins (084 D23): kill a running ``!`` command, pop a detail
        view, dismiss the question, interrupt the turn.

        When there is nothing to cancel, a second Esc within 0.5 s clears the input.
        (An open dialog is a modal screen and takes Esc before it reaches the app.)
        """
        controller = self.controller
        if controller is not None and controller.kill_shell():
            return
        if self._views:
            self.pop_view()
            self._last_esc_time = 0
            return
        action = controller.cancel() if controller is not None else None
        if action is not None:
            self._last_esc_time = 0
            return
        now = time.monotonic()
        if self._last_esc_time and now - self._last_esc_time < DOUBLE_ESC_S and self.input.text:
            self.input.clear()
            self._last_esc_time = 0
        else:
            self._last_esc_time = now

    # --- Detail views (084 D13) -------------------------------------------------------

    @property
    def views(self) -> tuple[DetailView, ...]:
        """The open detail views, bottom first; the last is on screen."""
        return tuple(self._views)

    @property
    def top_view(self) -> DetailView | None:
        """The detail view on screen, or None while the transcript shows."""
        return self._views[-1] if self._views else None

    @property
    def transcript(self) -> Widget:
        """What shows in the transcript's place: the top view's body, or the transcript."""
        top = self.top_view
        return top.body if top is not None else self.conversation

    def open_tool(self, tool: ToolNode) -> None:
        """Show a tool call's detail view, one level above the current one."""
        self._push(ToolDetailView(tool, self._crumbs()))

    def open_subagent(self, sub: SubagentNode) -> None:
        """Show a subagent's live transcript, one level above the current one."""
        self._push(SubagentView(sub, self._crumbs()))

    def on_tool_clicked(self, message: ToolClicked) -> None:
        """A tool row in the transcript or in a subagent view was clicked."""
        message.stop()
        self.open_tool(message.tool)

    def on_subagent_clicked(self, message: SubagentClicked) -> None:
        """A subagent row in the transcript or in a subagent view was clicked."""
        message.stop()
        self.open_subagent(message.sub)

    def _crumbs(self) -> list[Crumb]:
        top = self.top_view
        return list(top.crumbs) if top is not None else []

    def _focus_is_in_transcript(self) -> bool:
        """True when focus is nowhere, or on the transcript or a view (not the input)."""
        focused = self.app.focused
        if focused is None:
            return True
        chain = focused.ancestors_with_self
        return self.conversation in chain or any(view in chain for view in self._views)

    def _push(self, view: DetailView) -> None:
        """Hide what is shown (keeping it mounted) and show ``view`` in its place."""
        move_focus = self._focus_is_in_transcript()
        top = self.top_view
        if top is not None:
            top.display = False
        else:
            conversation = self.conversation
            self._saved_scroll = (conversation.scroll_y, conversation.following)
            conversation.display = False
        self._views.append(view)
        self.mount(view, before=self.activity)
        if move_focus:
            view.body.focus()

    def pop_view(self) -> None:
        """Close the top view and show the one under it, or the transcript where it was."""
        if not self._views:
            return
        move_focus = self._focus_is_in_transcript()
        self._views.pop().remove()
        top = self.top_view
        if top is not None:
            top.display = True
        else:
            self._show_transcript()
        if move_focus:
            self.transcript.focus()

    async def close_views(self) -> None:
        """Close every detail view and show the transcript."""
        views, self._views = self._views, []
        for view in reversed(views):
            await view.remove()
        if views:
            self._show_transcript()

    def _show_transcript(self) -> None:
        """Show the transcript again at the scroll position it was left at.

        One that was following the end goes to the end, past rows added meanwhile.
        Rows mounted while it was hidden are laid out only once it shows, which can
        take a few refreshes, so the end is re-checked until the height settles.
        """
        conversation = self.conversation
        conversation.display = True
        scroll_y, following = self._saved_scroll

        def restore(waits: int, height: int) -> None:
            if not conversation.is_attached or self.top_view is not None:
                return  # gone, or hidden again by a newer view
            if not following:
                conversation.scroll_to(y=scroll_y, animate=False, immediate=True)
                return
            conversation.scroll_end(animate=False, immediate=True)
            settled = conversation.virtual_size.height
            if waits > 0 and settled != height:
                self.call_after_refresh(restore, waits - 1, settled)

        self.call_after_refresh(restore, LAYOUT_WAITS, -1)

    @property
    def active_child_key(self) -> tuple[str, int] | None:
        """The subagent ``ctrl+x`` stops: the topmost open subagent view's."""
        for view in reversed(self._views):
            if isinstance(view, SubagentView):
                return view.child_key
        return None

    def stop_child(self) -> None:
        """Stop the subagent of the topmost open subagent view, if any."""
        key = self.active_child_key
        if key is not None and self.controller is not None:
            self.controller.stop_child(*key)
