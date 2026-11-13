"""One session's client state and behaviour, with no widgets (084 D6).

``SessionController`` owns everything the TUI knows about one session: the stream
connection and its reconnect rule, the replay cursor and event dedup, the transcript
model (``TranscriptBuilder``), accounting and status-bar values, turn state and turn
metrics, the pending question, the ``!`` direct shell, the client-only timeline
entries (turn status, client errors, shell output), and the message input's draft and
history while no view shows the session.

Direct shell (084 D20, D21): ``!command`` runs ``docker exec`` on this machine, so it
only reaches a local orchestrator's containers. A controller created with
``direct_shell=False`` (a remote profile) answers ``!`` with a client entry and runs
nothing.

It reports what changed to at most one attached ``SessionListener`` (the widgets that
show the session). The listener reads the rest from the controller's attributes, so a
view attached later can render the current state without replaying events.

Replay batching: ``replay`` brackets a history batch with ``on_replay(True)`` and
``on_replay(False)`` (``replaying`` is true in between) and yields to the event loop
every ``REPLAY_YIELD_EVERY`` events. A view suspends row mounting on the first call
and mounts the latest window once, from final node state, on the second.

Connection rule (084 D9): a lost stream (or a failed connect) is retried with a
0.5 → 8 s back-off for as long as the session is live: ``is_live`` (the app's feed
record) says so and ``end`` was not called. A 4004 close (the orchestrator has no such
session) stops at once. Each outage adds one ``Connection lost; reconnecting…`` client
entry, and only while a view is attached (background controllers retry silently); a
reconnect replays ``/events`` after the cursor to fill the gap. ``force_reconnect``
replaces a stream the host stalled (the app suspended for an external editor); while
``quiet`` is set, that outage adds no entry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from archie_shared import events as ce
from archie_shared.commands import AnswerCommand, InterruptCommand, SwitchModelCommand
from archie_shared.events import LLMRequest, decode_event
from archie_shared.kiro import kiro_model_id
from archie_shared.models import load_models
from archie_shared.protocol import PROTOCOL_VERSION
from archie_shared.transcript import Change, TranscriptBuilder

from archie_cli.tui.client import CursorNotFound, OrchestratorClient
from archie_cli.ws_client import WSClient

log = logging.getLogger(__name__)

SESSION_STATE_PROTOCOL = 4
"""First protocol version whose ``session_status`` carries state and question."""

REPLAY_YIELD_EVERY = 200
"""Events applied between yields to the event loop while a history batch replays."""

SHELL_TIMEOUT = 30
"""Seconds a ``!`` command may run before it is killed."""

SHELL_MAX_LINES = 10_000
"""Lines of ``!`` output kept; the rest is cut and marked ``(truncated)``."""

SEND_WAIT = 15.0
"""Seconds a send waits for a running reconnect before it fails."""

SHELL_NOT_LOCAL_TEXT = (
    "! commands need a local orchestrator: they run docker exec on this machine, "
    "and this window is connected to a remote one."
)
"""The client entry for a ``!`` command on a remote profile (084 D21)."""

RECONNECTING_TEXT = "Connection lost; reconnecting…"
"""The client entry an attached view gets once per outage."""

NOT_FOUND_TEXT = "The orchestrator no longer has this session; stopped reconnecting."
"""The client entry for a 4004 close."""

CLOSE_NOT_FOUND = 4004
"""Stream close code: the orchestrator has no such session."""

# ErrorNotice kinds that end the turn the client is waiting on.
_TURN_ENDING_NOTICES = frozenset(
    {"turn_active", "turn_error", "storage_error", "manual_compaction_failed"}
)

CancelAction = Literal["shell", "dismiss", "interrupt"]
"""What ``SessionController.cancel`` did: killed the shell, dismissed the question or
asked the agent to interrupt the turn."""

Activity = Literal["Thinking", "Working", "Responding"]
"""The activity line's word for a running turn."""


# --- Timeline entries ---------------------------------------------------------------


@dataclass(frozen=True)
class TurnStatusEntry:
    """Client-only metrics for a completed root turn."""

    anchor: str | None
    duration_s: float
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    cost: float
    credits: float | None
    kind: Literal["turn_status"] = "turn_status"


@dataclass(frozen=True)
class ClientErrorEntry:
    """A client or transport error; not part of the session log."""

    anchor: str | None
    message: str
    kind: Literal["client_error"] = "client_error"


@dataclass(frozen=True)
class ShellOutputEntry:
    """Output of a ``!`` command run on the host with ``docker exec``."""

    anchor: str | None
    command: str
    output: str
    exit_code: int
    kind: Literal["shell_output"] = "shell_output"


LocalEntry = TurnStatusEntry | ClientErrorEntry | ShellOutputEntry
"""A client-only timeline entry. ``anchor`` is the id of the last root row node in the
transcript when the entry was added (None before any), so a view rebuilt from the
builder can put the entry back after it."""


# --- Accounting -----------------------------------------------------------------------


@dataclass
class Ledger:
    """Session totals folded from ``llm_request`` ledger events."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0
    context_pct: float = 0.0
    cost: float = 0.0
    credits: float | None = None  # Kiro credits; None until a Kiro request reports them


@dataclass
class TurnMetrics:
    """Client-only accounting for the current root turn."""

    started_at: float | None = None  # monotonic time of a turn this client started
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    credits: float | None = None
    duration_s: float = 0.0  # summed request durations (turns this client did not start)

    def empty(self) -> bool:
        """True when nothing was measured, so no turn status row is shown."""
        return self.started_at is None and not any(
            (
                self.input_tokens,
                self.cache_read,
                self.cache_write,
                self.output_tokens,
                self.cost,
                self.credits,
            )
        )


def estimate_context_pct(context_tokens: int, model_key: str) -> float:
    """Context usage of ``context_tokens`` against the model's window, in percent."""
    model = load_models().get(model_key)
    if model is None or model.context <= 0:
        return 0.0
    return (context_tokens / model.context) * 100


def _add_credits(total: float | None, event: LLMRequest) -> float | None:
    """Add a request's Kiro credits to ``total``; native requests leave it unchanged."""
    credits = event.credits
    if not isinstance(credits, int | float):  # msgspec.UNSET on native requests
        return total
    return (total or 0.0) + credits


# --- Listener -------------------------------------------------------------------------


class SessionListener(Protocol):
    """What a view of a session is told; it reads current values from the controller."""

    def on_changes(self, changes: list[Change]) -> None:
        """The transcript model changed (``controller.builder``)."""
        ...

    def on_local(self, entry: LocalEntry) -> None:
        """A client-only timeline entry was added (also in ``controller.locals``)."""
        ...

    def on_status(self) -> None:
        """Status-bar values changed (model, branch, session id, ledger, output)."""
        ...

    def on_turn(self) -> None:
        """Turn state changed: ``thinking``, ``activity``, ``input_locked`` or ``turns_ended``."""
        ...

    def on_question(self) -> None:
        """The pending question was shown or cleared (``controller.question``)."""
        ...

    def on_loaded(self) -> None:
        """The first history replay landed."""
        ...

    async def on_reset(self) -> None:
        """A 409 replaced the builder and cleared accounting; drop rows built from it."""
        ...

    def on_replay(self, active: bool) -> None:
        """A history batch started (True) or finished (False)."""
        ...


# --- Controller -----------------------------------------------------------------------


class SessionController:
    """All client state and behaviour of one session (084 D6).

    Attributes:
        session_id: The orchestrator's id for the session (routing).
        container_name: The session's container, for ``!`` commands.
        direct_shell: ``!`` commands may run (the orchestrator is local, 084 D20).
        ws: The live stream, or None before the first connect.
        builder: The transcript model.
        locals: Client-only timeline entries, oldest first.
        cursor: Id of the latest persisted event applied (``/events?after=``).
        loaded: The first history replay has landed.
        replaying: A history batch is being applied.
        ledger: Session accounting totals.
        session_output: Output tokens shown, including the live streaming estimate.
        model_key: The session's current model key.
        model_name: Display name of the model, None until a status reports it.
        supports_cache: Whether the model caches, None until a status reports it.
        git_branch: Branch from the agent's status, None until reported.
        agent_session_id: Session id from the agent's handshake ("" until then).
        turn_active: A root turn is running.
        thinking: A provider request is outstanding.
        activity: The activity line's word for the turn, or None (see the property).
        input_locked: The message input is disabled.
        turns_ended: Count of turn teardowns; a view refocuses input when it grows.
        turn: Metrics of the current root turn.
        question: The pending question, if any.
        answer_in_flight: Id of the question an answer is being sent for.
        shell_active: A ``!`` command is running.
        quiet: Suppress connection errors and outage entries (the host was suspended).
        reconnecting: A reconnect attempt is running.
        started: ``start`` was called.
        live: The session is followed on its stream (False for an ended session,
            which only loads its log, and after ``end``).
        is_live: Whether the session's record says it is live; reconnects stop when
            it returns False (the app reads its feed record).
        draft: The message input's text, kept while no view is attached.
        history: Messages sent from the input, oldest first (input history).
    """

    RECONNECT_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)
    """Seconds before each reconnect attempt; the last repeats."""

    def __init__(
        self,
        client: OrchestratorClient,
        session_id: str,
        container_name: str,
        *,
        is_live: Callable[[], bool] | None = None,
        direct_shell: bool = True,
    ) -> None:
        """Create a controller; nothing connects until ``start``.

        Args:
            client: Access to the orchestrator.
            session_id: The session to follow.
            container_name: The session's container, for ``!`` commands.
            is_live: Whether the session's record says it is live; None means always.
            direct_shell: Whether ``!`` commands run; False on a remote profile, where
                ``docker exec`` can't reach the session's container.
        """
        self._client = client
        self.session_id = session_id
        self.container_name = container_name
        self.direct_shell = direct_shell
        self.ws: WSClient | None = None
        self.listener: SessionListener | None = None

        # History and model
        self.cursor: str | None = None
        self._seen_event_ids: set[str] = set()
        self.builder = TranscriptBuilder()
        self.locals: list[LocalEntry] = []
        self.loaded = False
        self.replaying = False

        # Accounting and status values
        self.ledger = Ledger()
        self._seen_accounted_ids: set[str] = set()
        self.estimated_output = 0  # live output estimate (chars/4), reset per request
        self.session_output = 0
        self.model_key = ""
        self.model_name: str | None = None
        self.supports_cache: bool | None = None
        self.git_branch: str | None = None
        self.agent_session_id = ""
        # Protocol of the agent from its handshake. Agents before v4 send
        # session_status without a state, which decodes as the "idle" default.
        self.agent_protocol = 0
        self._protocol_warned = False

        # Turn
        self.turn_active = False
        self.thinking = False
        self._phase: Activity = "Working"  # what the turn does once no request waits
        self.input_locked = False
        self.turns_ended = 0
        self.turn = TurnMetrics()

        # Question
        self.question: ce.PendingQuestion | None = None
        self.answer_in_flight: str | None = None

        # Direct shell
        self.shell_active = False
        self._shell_proc: asyncio.subprocess.Process | None = None
        self._shell_cancel_requested = False

        # Connection
        self.quiet = False
        self.closing = False
        self.reconnecting = False
        self._receive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._end_task: asyncio.Task | None = None
        self._buffering = False
        self._event_buffer: list = []
        self._tasks: set[asyncio.Task] = set()
        self.started = False
        self.live = True
        self.is_live: Callable[[], bool] = is_live or (lambda: True)
        self._outage = False  # an outage outlasted the first back-off delay
        self._outage_reported = False  # its client entry was added

        # Input, kept for the next view (084 D7)
        self.draft = ""
        self.history: list[str] = []

    @property
    def activity(self) -> Activity | None:
        """The activity line's word: what the running root turn is doing (084).

        ``Thinking`` while a provider request is outstanding and no text has arrived,
        ``Responding`` while a reply streams, ``Working`` while tool calls run. None
        between turns and while a question waits for the user (the question panel
        shows instead).
        """
        if not self.turn_active or self.question is not None:
            return None
        if self.thinking:
            return "Thinking"
        return self._phase

    @property
    def evictable(self) -> bool:
        """True when closing the controller loses no work in progress (084 D7).

        A running ``!`` command or an answer being sent keeps it.
        """
        return not self.shell_active and self.answer_in_flight is None

    # --- Listener -----------------------------------------------------------------

    def attach(self, listener: SessionListener) -> None:
        """Report changes to ``listener`` (replacing any attached one).

        A view attached during an outage gets the outage's client entry.
        """
        self.listener = listener
        if self._outage:
            self._report_outage()

    def detach(self) -> None:
        """Stop reporting changes; state keeps updating."""
        self.listener = None

    def _status_changed(self) -> None:
        if self.listener is not None:
            self.listener.on_status()

    def _turn_changed(self) -> None:
        if self.listener is not None:
            self.listener.on_turn()

    def _question_changed(self) -> None:
        if self.listener is not None:
            self.listener.on_question()

    def _set_replaying(self, active: bool) -> None:
        self.replaying = active
        if self.listener is not None:
            self.listener.on_replay(active)

    def _mark_loaded(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        if self.listener is not None:
            self.listener.on_loaded()

    # --- Timeline -----------------------------------------------------------------

    def _last_root_id(self) -> str | None:
        """Id of the last node the main transcript shows as a row, or None."""
        turn = self.builder.current_turn()
        if turn is not None:
            if turn.items:
                return turn.items[-1].id
            return turn.user.id if turn.user is not None else turn.id
        if self.builder.preamble:
            return self.builder.preamble[-1].id
        return None

    def _add_local(self, entry: LocalEntry) -> None:
        self.locals.append(entry)
        if self.listener is not None:
            self.listener.on_local(entry)

    def client_error(self, message: str) -> None:
        """Add a client/transport error entry (not recorded in the session log)."""
        self._add_local(ClientErrorEntry(self._last_root_id(), message))

    def _show_turn_status(self) -> None:
        """Add the client-only metrics entry for the completed root turn."""
        turn = self.turn
        if turn.empty():
            return
        duration_s = (
            time.monotonic() - turn.started_at if turn.started_at is not None else turn.duration_s
        )
        self._add_local(
            TurnStatusEntry(
                self._last_root_id(),
                duration_s,
                turn.input_tokens,
                turn.cache_read,
                turn.cache_write,
                turn.output_tokens,
                turn.cost,
                turn.credits,
            )
        )
        turn.started_at = None

    # --- Events -------------------------------------------------------------------

    def apply(self, event) -> None:
        """Apply one live or history event; both take the same path.

        Deduplicates by event id, advances the cursor for persisted events, updates
        the transcript model, then the client-side state (status, turn, question).
        """
        event_id = getattr(event, "id", None)
        if event_id is not None:
            if event_id in self._seen_event_ids:
                return
            self._seen_event_ids.add(event_id)
            if event.persist:
                self.cursor = event_id

        if isinstance(event, LLMRequest):
            self._accumulate_ledger(event)
        changes = self.builder.apply(event)
        if changes and self.listener is not None:
            self.listener.on_changes(changes)

        if (
            getattr(event, "scope", None) is not None
            and getattr(event, "subagent_index", None) is not None
        ):
            return  # child events only change the transcript

        if isinstance(event, ce.SessionStarted):
            self.model_key = event.model_key
        elif isinstance(event, ce.UserMessage):
            self.turn = TurnMetrics()
        elif isinstance(event, ce.Handshake):
            self.agent_session_id = event.session_id
            self.agent_protocol = event.protocol_version
            self._status_changed()
            if event.protocol_version > PROTOCOL_VERSION and not self._protocol_warned:
                self._protocol_warned = True
                self.client_error(
                    f"Protocol version mismatch: session uses v{event.protocol_version}, "
                    f"this client supports v{PROTOCOL_VERSION}. "
                    "Some features may not work — consider updating the CLI."
                )
        elif isinstance(event, ce.SessionStatus):
            self.model_key = event.model_key
            kiro_id = kiro_model_id(event.model_key)
            model = load_models().get(event.model_key)
            if kiro_id is not None:
                self.model_name = f"{kiro_id} (Kiro)"
                self.supports_cache = False
            elif model is not None:
                self.model_name = model.name
                self.supports_cache = model.can_cache
            self.git_branch = event.git_branch
            self._status_changed()
            if self.agent_protocol >= SESSION_STATE_PROTOCOL:
                self._apply_session_state(event)
        elif isinstance(event, ce.CompactionStart):
            self._stop_thinking()
        elif isinstance(event, ce.CompactionComplete):
            if event.tokens_after is not None:
                self._set_context(event.tokens_after)
            if event.reason == "manual":
                self._end_turn()
            else:
                # The conversation request that follows is outstanding again.
                self._start_thinking()
        elif isinstance(event, ce.ContextCompacted):
            self._stop_thinking()
            self._set_context(event.tokens_after)
            if event.reason == "manual":
                self._end_turn()
        elif isinstance(event, ce.ErrorNotice):
            self.client_error(event.message)
            if event.kind in _TURN_ENDING_NOTICES and self.turn_active:
                self._end_turn()
        elif isinstance(event, ce.TextDelta):
            if not changes:
                return  # a late delta for a request that is already final
            self.turn_active = True
            self._set_phase("Responding")
            self.estimated_output += len(event.text) // 4
            self.session_output = self.ledger.output_tokens + self.estimated_output
            self._status_changed()
        elif isinstance(event, ce.IterationStart):
            # A new iteration means a new provider request is outstanding.
            self._start_thinking()
        elif isinstance(event, ce.ToolCall):
            # The provider responded with a tool request: the turn now runs tools
            # until the next iteration's request.
            self._set_phase("Working")
        elif isinstance(event, ce.LLMRequest):
            turn = self.turn
            turn.input_tokens += event.input_tokens
            turn.cache_read += event.cache_read_tokens
            turn.cache_write += event.cache_write_tokens
            turn.output_tokens += event.output_tokens
            turn.cost += event.cost_usd
            turn.credits = _add_credits(turn.credits, event)
            turn.duration_s += event.duration_ms / 1000
        elif isinstance(event, (ce.TurnError, ce.TurnInterrupted)):
            self._end_turn()
        elif isinstance(event, ce.TurnComplete):
            self._show_turn_status()
            self._end_turn()

    def _set_context(self, tokens: int) -> None:
        """Record context size after compaction, against the session's model window."""
        self.ledger.context_tokens = tokens
        self.ledger.context_pct = estimate_context_pct(tokens, self.model_key)
        self._accounting_changed()

    def _accumulate_ledger(self, event: LLMRequest) -> None:
        """Fold an ``llm_request`` ledger event into session accounting.

        This is the authoritative cost and token source (it matches the persisted
        ledger). Deduplicated by event id so replay plus live can't double-count.
        """
        if event.id in self._seen_accounted_ids:
            return
        self._seen_accounted_ids.add(event.id)
        self.estimated_output = 0
        ledger = self.ledger
        ledger.input_tokens += event.input_tokens
        ledger.output_tokens += event.output_tokens
        ledger.cache_read += event.cache_read_tokens
        ledger.cache_write += event.cache_write_tokens
        if event.scope is None and event.subagent_index is None and event.purpose != "compaction":
            # Root conversation requests are persisted, so replay tracks model
            # switches even though live-only session_status frames are absent.
            self.model_key = event.model_key
            ledger.context_tokens = event.context_tokens
            ledger.context_pct = estimate_context_pct(event.context_tokens, event.model_key)
        ledger.cost += event.cost_usd
        ledger.credits = _add_credits(ledger.credits, event)
        self._accounting_changed()

    def _accounting_changed(self) -> None:
        self.session_output = self.ledger.output_tokens
        self._status_changed()

    # --- Turn state ---------------------------------------------------------------

    def _start_thinking(self) -> None:
        """Show the thinking indicator while a turn waits on the provider."""
        if self.thinking or not self.turn_active:
            return
        self.thinking = True
        self._turn_changed()

    def _stop_thinking(self) -> None:
        if self.thinking:
            self.thinking = False
            self._turn_changed()

    def _set_phase(self, phase: Activity) -> None:
        """The request answered with text or tool calls; report it if the word changed."""
        changed = self.thinking or phase != self._phase
        self.thinking = False
        self._phase = phase
        if changed:
            self._turn_changed()

    def _end_turn(self) -> None:
        """Single teardown path for every turn outcome."""
        self.thinking = False
        self.turn_active = False
        self._phase = "Working"
        self.input_locked = False
        self.turns_ended += 1
        self._turn_changed()

    # --- Question -----------------------------------------------------------------

    def _apply_session_state(self, event: ce.SessionStatus) -> None:
        """Follow the agent's live state: the pending question and whether a turn runs."""
        if event.state in ("working", "blocked") and not self.turn_active:
            # Joined mid-turn (reconnect or another client's turn): Esc must interrupt.
            self.turn_active = True
            self.input_locked = True
            self._turn_changed()
        if event.question is not None:
            self._show_question(event.question)
        elif self.question is not None:
            self._hide_question()
        if event.state == "idle" and self.turn_active:
            # A turn the client missed the end of (the terminal event normally ends it).
            self._end_turn()

    def _show_question(self, question: ce.PendingQuestion) -> None:
        self.question = question
        self.thinking = False
        self.input_locked = False
        self._question_changed()

    def _hide_question(self) -> None:
        self.question = None
        self.answer_in_flight = None
        if self.turn_active:
            self.input_locked = True
        self._question_changed()

    def answer(self, command: AnswerCommand) -> None:
        """Send an answer to the pending question unless one is already being sent."""
        if self.answer_in_flight == command.question_id:
            return
        self.answer_in_flight = command.question_id
        self._spawn(self._send_answer(command))

    async def _send_answer(self, command: AnswerCommand) -> None:
        """Send an answer; clear the question only once it was sent."""
        try:
            ws = await self._connected_ws()
            await ws.send_command(command)
        except Exception as e:  # noqa: BLE001 — surface any send failure
            self.answer_in_flight = None
            self.client_error(f"Send failed: {e}")
            return
        if self.question is not None and self.question.id == command.question_id:
            self._hide_question()

    # --- User actions -------------------------------------------------------------

    def submit(self, content: str) -> None:
        """Handle submitted input: a ``!`` command, an answer, or a new message.

        A ``!`` command runs only with ``direct_shell``; otherwise it adds a client
        entry saying why (084 D21).
        """
        if content.startswith("!"):
            command = content[1:].strip()
            if not command:
                return
            if not self.direct_shell:
                self.client_error(SHELL_NOT_LOCAL_TEXT)
                return
            if self.shell_active:
                self.client_error("Shell command already running")
                return
            self._spawn(self.run_shell(command))
            return

        if self.question is not None:
            self.answer(AnswerCommand(question_id=self.question.id, text=content))
            return

        if self.turn_active:
            return

        self.turn_active = True
        self.turn = TurnMetrics(started_at=time.monotonic())
        self.thinking = True
        self.input_locked = True
        self._turn_changed()
        self._spawn(self._send_message(content))

    async def _send_message(self, content: str) -> None:
        """Reconnect if needed, then send a message to the agent."""
        try:
            ws = await self._connected_ws()
            await ws.send_message(content)
        except Exception as e:  # noqa: BLE001 — any failure ends the turn
            self.client_error(f"Send failed: {e}")
            self._end_turn()
            # A send failure means the socket is dead; try to recover.
            self._schedule_reconnect()

    def kill_shell(self) -> bool:
        """Kill a running ``!`` command; False when none is running."""
        if self.shell_active and self._shell_proc is not None:
            self._shell_cancel_requested = True
            self._shell_proc.kill()
            return True
        return False

    def cancel(self) -> CancelAction | None:
        """Esc: kill a running ``!`` command, dismiss the question, or interrupt the turn.

        Returns:
            What was done, or None when there was nothing to cancel.
        """
        if self.kill_shell():
            return "shell"
        if self.question is not None:
            # The first Esc dismisses the question; the next one interrupts the turn.
            self.answer(AnswerCommand(question_id=self.question.id, dismiss=True))
            return "dismiss"
        if self.turn_active:
            self._fire(lambda ws: ws.send_interrupt())
            return "interrupt"
        return None

    def stop_child(self, scope: str, index: int) -> None:
        """Interrupt one subagent while its siblings keep running."""
        self._fire(lambda ws: ws.send_command(InterruptCommand(scope=scope, subagent_index=index)))

    def switch_model(self, model_key: str) -> bool:
        """Ask the agent to switch model; False (nothing sent) while a turn runs."""
        if self.turn_active:
            return False
        self._fire(lambda ws: ws.send_command(SwitchModelCommand(model_key=model_key)))
        return True

    def _fire(self, make: Callable[[WSClient], Awaitable[None]]) -> None:
        """Run a send on the current stream in the background (no reconnect first)."""
        ws = self.ws
        if ws is None:
            log.debug("Session %s is not connected; command dropped", self.session_id)
            return

        async def run() -> None:
            await make(ws)

        self._spawn(run())

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- Direct shell -------------------------------------------------------------

    async def run_shell(self, command: str) -> None:
        """Run ``command`` in the session's container with host-side ``docker exec``.

        The output becomes a local entry; nothing is sent to the agent or logged.
        """
        self.shell_active = True
        self._shell_cancel_requested = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                "-w",
                "/workspace",
                self.container_name,
                "bash",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._shell_proc = proc
            try:
                stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
                exit_code = 130 if self._shell_cancel_requested else (proc.returncode or 0)
                output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            except TimeoutError:
                proc.kill()
                await proc.wait()
                exit_code = 124
                output = "(timed out)"

            lines = output.split("\n")
            if len(lines) > SHELL_MAX_LINES:
                output = "\n".join(lines[:SHELL_MAX_LINES]) + "\n(truncated)"
            self._add_local(ShellOutputEntry(self._last_root_id(), command, output, exit_code))
        except Exception as exc:  # noqa: BLE001 — local execution failure
            self.client_error(f"Docker exec failed: {exc}")
        finally:
            self.shell_active = False
            self._shell_proc = None
            self._shell_cancel_requested = False

    # --- History ------------------------------------------------------------------

    async def replay(self) -> bool:
        """Fetch and apply persisted events after the cursor (all of them without one).

        A cursor the log no longer has (409) resets every replay-derived state and
        refetches the whole log.

        Returns:
            False when the history could not be read. Callers must not flush
            buffered live events or report a reconnect then, because those events
            cannot fill the history gap.
        """
        try:
            try:
                body = await self._client.events(self.session_id, self.cursor)
            except CursorNotFound:
                # The cursor was rotated or archived away: rebuild from the full log.
                await self.reset()
                body = await self._client.events(self.session_id, None)
        except Exception as e:  # noqa: BLE001 — any failure means no history
            log.warning("Failed to replay events: %s", e)
            return False

        self._set_replaying(True)
        try:
            applied = 0
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = decode_event(line, persisted=True)
                except Exception as e:  # noqa: BLE001 — skip malformed replay lines
                    log.warning("Skipping malformed replay event: %s", e)
                    continue
                self.apply(event)
                applied += 1
                if applied % REPLAY_YIELD_EVERY == 0:
                    await asyncio.sleep(0)
        finally:
            self._set_replaying(False)
        return True

    async def reset(self) -> None:
        """Clear all state reconstructed from the persisted event stream.

        Local entries are kept; their anchors may no longer exist.
        """
        self.cursor = None
        self._seen_event_ids.clear()
        self.builder = TranscriptBuilder()
        self._seen_accounted_ids.clear()
        self.ledger = Ledger()
        self.estimated_output = 0
        self.turn = TurnMetrics()
        self.turn_active = False
        if self.listener is not None:
            await self.listener.on_reset()
        self._accounting_changed()

    # --- Connection ---------------------------------------------------------------

    async def start(self, *, live: bool = True) -> None:
        """Connect, replay history while live frames buffer, then follow the stream.

        Ordering: open the stream → start the receive loop (buffering) → replay
        ``/events`` → apply buffered live frames not already applied (by event id).
        A failed connect or replay is retried like a lost stream (084 D9).

        Args:
            live: False for an ended session: load ``/events`` once and open no stream
                (084 D9).
        """
        self.started = True
        self.live = live
        if not live:
            await self._load_log()
            return
        try:
            ws = await self._client.stream(self.session_id)
        except Exception as e:  # noqa: BLE001 — any connect failure starts an outage
            log.warning("Session %s connect failed: %s", self.session_id, e)
            self._schedule_reconnect()
            return
        self.ws = ws
        if not self.live:  # ended while connecting
            await self._disconnect()
            await self._load_log()
            return

        self._event_buffer = []
        self._buffering = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        if not await self.replay():
            self._buffering = False
            self._event_buffer = []
            if not self.quiet:
                self.client_error("Initial event replay failed; retrying connection.")
                self._outage_reported = True  # this outage's one entry
            await self._cancel_receive()
            await self._disconnect()
            self._schedule_reconnect()
            return

        self._buffering = False
        self._dispatch_buffered()
        self._event_buffer = []
        self._mark_loaded()

    async def _load_log(self) -> None:
        """Load an ended session's history (no stream)."""
        if await self.replay():
            self._mark_loaded()
        else:
            self.client_error("Could not load the session's history.")

    def end(self) -> None:
        """The session ended: stop following its stream and stop reconnecting.

        The transcript and every other state are kept. In the background the stream is
        closed and, once history has loaded, ``/events`` is read once more after the
        cursor, so events the stream did not deliver (an outage before the end) show.
        A turn still running is ended and a pending question cleared. Calling it again,
        or on a controller that only loads a log, does nothing.
        """
        if not self.live or self.closing:
            return
        self.live = False
        self._outage = False
        self._end_task = asyncio.create_task(self._finish())

    async def _finish(self) -> None:
        current = asyncio.current_task()
        task = self._reconnect_task
        if task is not None and task is not current and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — ending must continue
                log.debug("Session %s reconnect ended: %s", self.session_id, exc)
        self.reconnecting = False
        self._buffering = False
        self._event_buffer = []
        await self._cancel_receive()
        await self._disconnect()
        if self.loaded and not self.replaying and not self.closing:
            await self.replay()
        if self.question is not None:
            self._hide_question()
        if self.turn_active:
            self._end_turn()

    async def _disconnect(self) -> None:
        """Close the stream, if any; failures are only logged."""
        ws = self.ws
        if ws is None:
            return
        try:
            await ws.disconnect()
        except Exception as exc:  # noqa: BLE001 — the stream is being dropped anyway
            log.debug("Session %s stream close failed: %s", self.session_id, exc)

    def _dispatch_buffered(self) -> None:
        """Apply live frames received during replay; event ids suppress duplicates."""
        for event in self._event_buffer:
            self.apply(event)

    async def _cancel_receive(self) -> None:
        task = self._receive_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _receive_loop(self) -> None:
        """Apply frames from the stream (buffered while a replay runs).

        When the stream drops, start a reconnect (084 D9); a 4004 close stops instead.
        A clean shutdown (``close``) or an ended session does neither.
        """
        ws = self.ws
        if ws is None:
            return
        try:
            async for event in ws.receive():
                if self._buffering:
                    self._event_buffer.append(event)
                else:
                    self.apply(event)
        except Exception as e:  # noqa: BLE001 — any receive failure is a lost connection
            log.warning("Session %s stream lost: %s", self.session_id, e)
            close_code = getattr(getattr(e, "rcvd", None), "code", None)
            if close_code == CLOSE_NOT_FOUND:
                if self.following and not self.quiet and self.listener is not None:
                    self.client_error(NOT_FOUND_TEXT)
                return
            self._schedule_reconnect()
            return

        # The generator ended without raising: the connection closed underneath us.
        log.info("Session %s stream closed", self.session_id)
        self._schedule_reconnect()

    @property
    def following(self) -> bool:
        """True while the stream should be followed: live and not closing."""
        return self.live and not self.closing and self.is_live()

    def _schedule_reconnect(self, *, fresh: bool = False) -> None:
        """Start a background reconnect unless one runs or the session isn't live.

        Args:
            fresh: Close the current stream first and try at once (``force_reconnect``).
        """
        if self.reconnecting or not self.following:
            return
        self.reconnecting = True
        task = asyncio.create_task(self._reconnect(fresh=fresh))
        self._reconnect_task = task

        def clear_reconnect_task(done: asyncio.Task) -> None:
            if self._reconnect_task is done:
                self._reconnect_task = None

        task.add_done_callback(clear_reconnect_task)

    def resume(self) -> None:
        """Reconnect a live session whose stream is down and not being retried.

        The app calls this when its feed re-syncs, so a stream that stopped (a 4004 or
        a failed send) is tried again while the record says the session is live.
        """
        if not self.started or (self.ws is not None and self.ws.connected):
            return
        self._schedule_reconnect()

    async def ensure_connected(self) -> bool:
        """Make sure the stream is connected before sending a user command.

        Starts a reconnect if none runs and waits up to ``SEND_WAIT`` seconds for it.
        """
        if not self.reconnecting and self.ws is not None and self.ws.connected:
            return True  # (a stream being replaced may still report connected)
        self._schedule_reconnect()
        return await self.wait_connected(SEND_WAIT)

    async def wait_connected(self, timeout: float = SEND_WAIT) -> bool:
        """Wait up to ``timeout`` seconds for a running reconnect to finish.

        Returns:
            True when the stream is connected and no reconnect runs.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.reconnecting and not self.closing and loop.time() < deadline:
            await asyncio.sleep(0.05)
        return not self.reconnecting and self.ws is not None and self.ws.connected

    async def _connected_ws(self) -> WSClient:
        if not await self.ensure_connected() or self.ws is None:
            raise RuntimeError("Not connected")
        return self.ws

    def force_reconnect(self) -> None:
        """Replace the stream with a fresh connection (after the host was suspended).

        A suspended event loop may leave the socket reporting OPEN after its
        keepalive expired, so the current connection is not trusted: the reconnect
        closes it and tries at once, then backs off as usual. It is under way when this
        returns, so a send made afterwards waits for the new stream. Does nothing for a
        controller that was not started, no longer follows its stream, or already
        reconnects. ``wait_connected`` waits for the outcome.
        """
        if not self.started or self.reconnecting or not self.following:
            return
        self._schedule_reconnect(fresh=True)

    def _report_outage(self) -> None:
        """Add the outage's one client entry, if a view is attached (084 D9)."""
        if self._outage_reported or self.quiet or self.listener is None:
            return
        self._outage_reported = True
        self.client_error(RECONNECTING_TEXT)

    async def _reconnect(self, *, fresh: bool = False) -> bool:
        """Reconnect with back-off and resync through ``/events?after=<cursor>``.

        Retries with ``RECONNECT_DELAYS`` (the last repeating) while ``following``;
        the first wait that passes marks an outage, which an attached view is told
        about once. On success the receive loop restarts and missed events replay,
        deduplicated by event id.

        Args:
            fresh: Close the current stream first and make the first attempt at once.

        Returns:
            True once reconnected; False when the session stopped being live.
        """
        attempt = 0
        waits = 0
        try:
            if fresh:
                await self._cancel_receive()
                await self._disconnect()
            while self.following:
                if not (fresh and attempt == 0):
                    delay = self.RECONNECT_DELAYS[min(waits, len(self.RECONNECT_DELAYS) - 1)]
                    waits += 1
                    await asyncio.sleep(delay)
                    if not self.following:
                        break
                    self._outage = True
                    self._report_outage()
                attempt += 1
                try:
                    ws = await self._client.stream(self.session_id)
                except Exception as e:  # noqa: BLE001 — retry on any connect failure
                    log.warning("Reconnect attempt %d failed: %s", attempt, e)
                    continue
                self.ws = ws

                # Reconnected: buffer live frames, replay missed events (deduplicated
                # by id), then apply the buffered frames.
                self._event_buffer = []
                self._buffering = True
                await self._cancel_receive()  # a stale loop must not race the new one
                self._receive_task = asyncio.create_task(self._receive_loop())
                if not await self.replay():
                    self._buffering = False
                    self._event_buffer = []
                    await self._cancel_receive()
                    await self._disconnect()
                    continue
                self._buffering = False
                self._dispatch_buffered()
                self._event_buffer = []
                self._outage = False
                self._outage_reported = False
                self._mark_loaded()
                return True
            return False
        finally:
            self.reconnecting = False

    async def close(self) -> None:
        """Stop following the session: cancel receive and reconnect, then disconnect."""
        self.closing = True
        current = asyncio.current_task()
        tasks = (self._receive_task, self._reconnect_task, self._end_task)
        for task in tasks:
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in tasks:
            if task is None or task is current or task.done():
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — shutdown must continue
                log.debug("Session client task ended during shutdown: %s", exc)
        self._receive_task = None
        self._reconnect_task = None
        self.reconnecting = False

        if self.ws is not None:
            try:
                await self.ws.disconnect()
            except Exception as exc:  # noqa: BLE001 — shutdown must continue
                log.debug("Session WebSocket ended during shutdown: %s", exc)
