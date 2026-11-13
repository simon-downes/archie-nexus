"""The session pane: one session's header, transcript, question, input and status bar.

``SessionPane`` is the view of one ``SessionController`` (084 D6). ``attach`` renders
the controller's current state (the transcript through ``Conversation.load``, status,
turn, question, and the input's draft and history), then the pane receives the
controller's changes as its ``SessionListener``. ``detach`` hands the input's draft and
history back to the controller, so the next pane for the session restores them.

Every query is scoped to the pane: the app mounts one pane at a time (D7), but nothing
here assumes that. Tool and subagent popups are still modal ``DetailScreen``s on the
app's screen stack (in-pane views are M5); the pane opens them and feeds them its model
changes.
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
from archie_cli.tui.conversation import format_cost
from archie_cli.tui.detail import DetailScreen, SubagentScreen, ToolDetailScreen
from archie_cli.tui.input import MessageInput
from archie_cli.tui.question import QuestionPanel
from archie_cli.tui.session import LocalEntry, SessionController
from archie_cli.tui.sidebar import ANIMATED_STATUSES, branch_label, state_badge, state_glyph
from archie_cli.tui.status import StatusBar
from archie_cli.tui.throbber import Throbber, ThrobberContainer
from archie_cli.tui.transcript_view import Conversation, SubagentClicked, ToolClicked

DOUBLE_ESC_S = 0.5
"""Seconds within which a second Esc clears the input."""


class SessionHeader(Widget):
    """Above the transcript: state glyph and name on the left, the state badge right."""

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
        self.frame = 0

    @property
    def animated(self) -> bool:
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


class SessionPane(Vertical):
    """The on-screen session: header, transcript, throbber, question, input, status bar.

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
        self.throbber = ThrobberContainer()
        self.question = QuestionPanel()
        self.input = MessageInput()
        self.status = StatusBar()
        self._turns_ended = 0
        self._last_esc_time = 0.0

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.loading_line
        yield self.conversation
        yield self.throbber
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
        self.input._history = list(controller.history)
        self.input._history_idx = len(self.input._history)
        self._turns_ended = controller.turns_ended
        self._show_loading()
        self._show_branch()
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
        controller.history = list(self.input._history)
        self.controller = None

    def update_record(self, record: SessionDescriptor) -> None:
        """Show a newer record of the session (header, loading line, input, branch)."""
        self.record = record
        self.header.record = record
        self.header.refresh()
        self._show_branch()
        self._show_loading()
        self._show_turn()

    def _show_branch(self) -> None:
        """The status bar's branch, from the record's location (084 D31)."""
        self.status.git_branch = branch_label(self.record.location) or "—"

    def tick(self, frame: int) -> bool:
        """Advance the header's glyph; True if it animates."""
        if not self.header.animated:
            return False
        self.header.frame = frame
        self.header.refresh()
        return True

    @property
    def animated(self) -> bool:
        """True while the header's glyph animates."""
        return self.header.animated

    def focus_input(self) -> None:
        """Focus the input when it takes text."""
        if not self.input.disabled:
            self.input.focus()

    def _show_loading(self) -> None:
        controller = self.controller
        if self.record.status == "starting":
            text = "Starting…"
        elif controller is None or not controller.loaded:
            text = "Loading…"
        else:
            text = ""
        self.loading_line.update(Text(text, style=theme.MUTED))
        self.loading_line.display = bool(text)

    def _show_turn(self) -> None:
        controller = self.controller
        if controller is None:
            return
        self.query_one(Throbber).display = controller.thinking
        self.input.disabled = controller.input_locked or self.record.ended

    # --- SessionListener ----------------------------------------------------------

    def on_changes(self, changes: list[Change]) -> None:
        """Send model changes to the transcript and every open popup."""
        controller = self.controller
        if controller is None:
            return
        self.conversation.apply_changes(controller.builder, changes)
        for screen in self._detail_screens():
            screen.node_changed(controller.builder, changes)

    def on_local(self, entry: LocalEntry) -> None:
        """Append a client-only row."""
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
        if controller.agent_session_id:
            status.session_id = controller.agent_session_id
        ledger = controller.ledger
        status.session_input = ledger.input_tokens
        status.session_output = controller.session_output
        status.cache_read = ledger.cache_read
        status.cache_write = ledger.cache_write
        status.context_tokens = ledger.context_tokens
        status.context_pct = ledger.context_pct
        status.pricing_label = format_cost(ledger.cost, ledger.credits)

    def on_turn(self) -> None:
        """Show the throbber and input state; refocus the input after a turn ends."""
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
        """Close popups of the old model and drop every transcript row."""
        while isinstance(self.app.screen, DetailScreen):
            await self.app.pop_screen()
        await self.conversation.reset()

    def on_replay(self, active: bool) -> None:
        """Mount rows once per history batch, from final node state."""
        if active:
            self.conversation.suspend()
        else:
            self.conversation.resume()

    def on_notice(self, message: str, timeout: float | None) -> None:
        """Show a transient connection notice."""
        if timeout is None:
            self.app.notify(message)
        else:
            self.app.notify(message, timeout=timeout)

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
        """Esc: kill a running ``!`` command, dismiss the question, or interrupt.

        When there is nothing to cancel, a second Esc within 0.5 s clears the input.
        """
        controller = self.controller
        action = controller.cancel() if controller is not None else None
        if action == "shell":
            return
        if action is not None:
            self._last_esc_time = 0
            return
        now = time.monotonic()
        if self._last_esc_time and now - self._last_esc_time < DOUBLE_ESC_S and self.input.text:
            self.input.clear()
            self._last_esc_time = 0
        else:
            self._last_esc_time = now

    # --- Popups -------------------------------------------------------------------

    def _detail_screens(self) -> list[DetailScreen]:
        return [screen for screen in self.app.screen_stack if isinstance(screen, DetailScreen)]

    def _crumbs(self) -> list[str]:
        """Breadcrumb levels for a popup opened from the current screen."""
        top = self.app.screen
        if isinstance(top, DetailScreen):
            return list(top.crumbs)
        controller = self.controller
        return [(controller.agent_session_id if controller else "") or "Session"]

    def open_tool(self, tool: ToolNode) -> None:
        """Open the detail popup of a tool call."""
        self.app.push_screen(ToolDetailScreen(tool, self._crumbs()))

    def open_subagent(self, sub: SubagentNode) -> None:
        """Open the live transcript popup of a subagent."""
        self.app.push_screen(SubagentScreen(sub, self._crumbs()))

    def on_tool_clicked(self, message: ToolClicked) -> None:
        message.stop()
        self.open_tool(message.tool)

    def on_subagent_clicked(self, message: SubagentClicked) -> None:
        message.stop()
        self.open_subagent(message.sub)

    @property
    def active_child_key(self) -> tuple[str, int] | None:
        """The subagent ``ctrl+x`` stops: the topmost open subagent popup's."""
        for screen in reversed(self._detail_screens()):
            if isinstance(screen, SubagentScreen):
                return (screen.sub.scope, screen.sub.index)
        return None

    def stop_child(self) -> None:
        """Stop the subagent of the topmost open subagent popup, if any."""
        key = self.active_child_key
        if key is not None and self.controller is not None:
            self.controller.stop_child(*key)

    async def close_popups(self) -> None:
        """Close every tool and subagent popup (the session is leaving the screen)."""
        while isinstance(self.app.screen, DetailScreen):
            await self.app.pop_screen()
