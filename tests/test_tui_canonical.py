"""TUI-side canonical event handling: ledger accounting, reducer, replay, local entries.

State is asserted on ``SessionController`` with a recording listener and the fake
orchestrator client (``tests/tui_fakes.py``); rendering through a mounted
``Conversation`` fed by the controller's changes; status-bar text on the app's pane.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from archie_cli.tui.client import CursorNotFound, HttpOrchestrator, OrchestratorError
from archie_cli.tui.session import (
    RECONNECTING_TEXT,
    ClientErrorEntry,
    ShellOutputEntry,
    TurnStatusEntry,
)
from archie_shared.events import (
    AssistantMessage,
    CompactionComplete,
    CompactionStart,
    ContextCompacted,
    ErrorNotice,
    Handshake,
    IterationStart,
    LLMRequest,
    SessionStatus,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserMessage,
)
from archie_shared.transcript import TranscriptBuilder

from tests.tui_fakes import (
    FakeOrchestrator,
    RecordingListener,
    attached,
    local_texts,
    make_controller,
)


def _llm_request(event_id, cost, input_tokens=100, output_tokens=10):
    return LLMRequest(
        id=event_id,
        scope=None,
        turn=1,
        iteration=1,
        model_key="m",
        sent_at="2025-01-01T00:00:00+00:00",
        duration_ms=12,
        status="completed",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=2,
        cache_write_tokens=1,
        context_tokens=input_tokens + output_tokens,
        cost_usd=cost,
    )


def _user(content="go", event_id="u1"):
    return UserMessage(id=event_id, turn=1, scope=None, content=content)


def _assistant(event_id="a1", content="ok", request_id="r1"):
    return AssistantMessage(
        id=event_id,
        turn=1,
        scope=None,
        iteration=1,
        request_id=request_id,
        content=content,
        interrupted=False,
    )


def _root_turn_complete(stop_reason):
    return TurnComplete(id=f"tc-{stop_reason}", turn=1, scope=None, stop_reason=stop_reason)


# --- Accounting -------------------------------------------------------------------


def test_accumulate_ledger_folds_cost_and_tokens():
    controller, listener = make_controller()
    controller.apply(_llm_request("e1", 0.10, input_tokens=100, output_tokens=10))
    controller.apply(_llm_request("e2", 0.05, input_tokens=50, output_tokens=5))

    assert controller.ledger.cost == pytest.approx(0.15)
    assert controller.ledger.input_tokens == 150
    assert controller.ledger.output_tokens == 15
    assert controller.session_output == 15
    assert listener.names().count("status") == 2


def test_child_ledger_does_not_overwrite_root_context():
    controller, _ = make_controller()
    root = _llm_request("root", 0.01, input_tokens=1000, output_tokens=10)
    child = LLMRequest(
        id="child",
        scope="task-1",
        subagent_index=0,
        turn=1,
        iteration=0,
        model_key="m",
        sent_at="2025-01-01T00:00:00+00:00",
        duration_ms=12,
        status="completed",
        input_tokens=9000,
        output_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=9000,
        cost_usd=0.02,
    )
    controller.apply(root)
    controller.apply(child)

    assert controller.ledger.context_tokens == 1010
    assert controller.ledger.input_tokens == 10000
    assert controller.ledger.cost == pytest.approx(0.03)


def test_accumulate_ledger_deduplicates_by_id():
    """Replay + live broadcast of the same llm_request must not double-count."""
    controller, _ = make_controller()
    controller._accumulate_ledger(_llm_request("e1", 0.10))
    controller._accumulate_ledger(_llm_request("e1", 0.10))  # duplicate id

    assert controller.ledger.cost == pytest.approx(0.10)
    assert controller.ledger.input_tokens == 100


def test_format_cost_adds_credits_only_when_reported():
    from archie_cli.tui.conversation import format_cost

    assert format_cost(0.0753) == "$0.0753"
    assert format_cost(0.0753, 3.764829) == "$0.0753 · 3.76 credits"
    assert format_cost(0.0, 0.0) == "$0.0000 · 0.00 credits"


def _host_app():
    from tests.tui_fakes import make_app, make_record

    return make_app(FakeOrchestrator(sessions=[make_record("proj-01abc")]), focus="proj-01abc")


@pytest.mark.asyncio
async def test_accumulate_ledger_sums_kiro_credits_into_status_label():
    from archie_cli.tui.status import StatusBar

    app = _host_app()
    first = msgspec.structs.replace(_llm_request("k1", 0.0097), credits=0.4828)
    second = msgspec.structs.replace(_llm_request("k2", 0.0753), credits=3.764829)
    async with app.run_test(size=(150, 30)) as pilot:
        await attached(app, pilot)
        app.controller.apply(first)
        app.controller.apply(second)
        app.controller._accumulate_ledger(second)  # a duplicate id is not counted twice
        await pilot.pause()
        assert app.controller.ledger.credits == pytest.approx(4.247629)
        assert app.pane.query_one(StatusBar).pricing_label == "$0.0850 · 4.25 credits"


@pytest.mark.asyncio
async def test_accumulate_ledger_native_requests_show_no_credits():
    from archie_cli.tui.status import StatusBar

    app = _host_app()
    async with app.run_test(size=(150, 30)) as pilot:
        await attached(app, pilot)
        app.controller.apply(_llm_request("e1", 0.10))
        await pilot.pause()
        assert app.controller.ledger.credits is None
        assert app.pane.query_one(StatusBar).pricing_label == "$0.1000"


@pytest.mark.parametrize(
    ("credits", "expected"),
    [(3.764829, pytest.approx(3.764829)), (None, None)],
    ids=["kiro", "native"],
)
def test_turn_status_receives_turn_credits(credits, expected):
    controller, listener = make_controller()
    request = _llm_request("r1", 0.0753)
    if credits is not None:
        request = msgspec.structs.replace(request, credits=credits)
    controller.apply(_user())
    controller.apply(request)
    controller.apply(_root_turn_complete("end_turn"))

    (entry,) = [e for (e,) in listener.of("local")]
    assert isinstance(entry, TurnStatusEntry)
    assert entry.cost == pytest.approx(0.0753)
    assert entry.credits == expected

    controller.apply(_user("next", event_id="u2"))
    assert controller.turn.credits is None


def test_accumulate_ledger_resets_streaming_output_estimate():
    controller, _ = make_controller()
    controller.estimated_output = 25
    controller.apply(_llm_request("e1", 0.10))
    assert controller.estimated_output == 0


def test_compaction_request_counts_cost_without_context_update():
    controller, _ = make_controller()
    controller.ledger.context_tokens = 77
    event = _llm_request("compact", 0.25, input_tokens=500)
    event.purpose = "compaction"
    controller.apply(event)
    assert controller.ledger.cost == pytest.approx(0.25)
    assert controller.ledger.context_tokens == 77


# --- Rendering through a mounted Conversation ------------------------------------


def _compaction_start(reason="cold_cache", estimate=2.54, request_id="r1"):
    return CompactionStart(
        id=f"cs-{request_id}",
        turn=1,
        iteration=0,
        scope=None,
        request_id=request_id,
        reason=reason,
        tokens_before=123457,
        estimated_next_request_cost_usd=estimate,
        model_key="m",
        sent_at="now",
    )


def _compaction_complete(
    reason="cold_cache",
    status="success",
    *,
    request_id="r1",
    tokens_after=4563,
    cost_saved=0.61,
    estimate=2.54,
    error=None,
):
    return CompactionComplete(
        id=f"cc-{request_id}",
        turn=1,
        iteration=0,
        scope=None,
        request_id=request_id,
        reason=reason,
        status=status,
        tokens_before=123457,
        tokens_after=tokens_after,
        cost_saved_usd=cost_saved,
        duration_ms=123,
        error=error,
        summary="summary" if status == "success" else None,
        estimated_next_request_cost_usd=estimate,
    )


def _rows(conv):
    """Mounted top-level row widgets of a conversation, in order."""
    return [row.widget for row in conv.rows if row.widget is not None]


class _ViewListener(RecordingListener):
    """Records calls and renders model changes into a mounted Conversation."""

    def __init__(self, controller, conv) -> None:
        super().__init__()
        self._controller = controller
        self._conv = conv

    def on_changes(self, changes) -> None:
        super().on_changes(changes)
        self._conv.apply_changes(self._controller.builder, changes)


class _ConversationHost:
    """A mounted Conversation driven by a SessionController."""

    def __init__(self) -> None:
        from archie_cli.tui.transcript_view import Conversation
        from textual.app import App

        class Host(App):
            def compose(self):
                yield Conversation(id="conversation")

        self.host = Host()
        self.controller, _ = make_controller()

    async def run(self, events, *, turn_active=False, inspect=None):
        """Apply events one by one; return per-event compaction headers, or (with
        ``inspect``) per-event ``inspect(conv)``."""
        from archie_cli.tui.conversation import CompactionEntry
        from archie_cli.tui.transcript_view import Conversation

        async with self.host.run_test() as pilot:
            conv = self.host.query_one(Conversation)
            self.controller.attach(_ViewListener(self.controller, conv))
            self.controller.turn_active = turn_active
            snapshots = []
            for event in events:
                self.controller.apply(event)
                await pilot.pause()
                if inspect is not None:
                    snapshots.append(inspect(conv))
                else:
                    snapshots.append([_header_text(entry) for entry in conv.query(CompactionEntry)])
            return snapshots


def _header_text(entry) -> str:
    from textual.widgets import Static

    return str(entry.query_one(".tool-header", Static).render())


def _span_styles(static) -> str:
    """Lower-cased styles of a Static's Text content, joined for substring checks."""
    return " ".join(str(span.style) for span in static.content.spans).lower()


async def test_live_assistant_message_finalizes_stream_without_duplicate():
    """The persisted assistant record must reconcile the live text stream."""
    from archie_cli.tui.conversation import AssistantMessage as AssistantRow
    from archie_cli.tui.conversation import StreamingMessage

    text_delta = TextDelta(id="d1", turn=1, iteration=1, scope=None, request_id="r1", text="hello")
    host = _ConversationHost()
    conv = await host.run([_user(), text_delta, _assistant(content="hello")], inspect=_rows)
    rows = conv[-1]
    assert [type(r).__name__ for r in rows] == ["UserMessage", "StreamingMessage"]
    stream = rows[1]
    assert isinstance(stream, StreamingMessage) and not isinstance(stream, AssistantRow)
    assert stream.text == "hello"
    (item,) = host.controller.builder.turns[0].items
    assert item.kind == "text" and item.content == "hello"


def test_activity_tracks_provider_request_lifecycle():
    """Thinking while a provider request is pending, Responding while text streams,
    Working while tools execute; nothing between turns."""
    controller, listener = make_controller()
    assert controller.activity is None
    controller.turn_active = True

    controller.apply(IterationStart(id="i1", turn=1, iteration=0, scope=None))
    assert controller.thinking is True
    assert controller.activity == "Thinking"

    controller.apply(
        TextDelta(id="d1", turn=1, iteration=0, scope=None, request_id="r1", text="partial")
    )
    assert controller.thinking is False
    assert controller.activity == "Responding"

    controller.apply(IterationStart(id="i2", turn=1, iteration=1, scope=None))
    assert controller.thinking is True
    assert controller.activity == "Thinking"
    controller.apply(
        ToolCall(
            id="c1",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r2",
            tool_use_id="t1",
            name="read",
            input={"path": "/etc/hosts"},
        )
    )
    assert controller.thinking is False
    assert controller.activity == "Working"
    assert listener.names().count("turn") == 4


async def test_cold_cache_compaction_renders_pending_then_success():
    host = _ConversationHost()
    headers = await host.run([_compaction_start(), _compaction_complete()])
    assert headers == [
        ["○ Compaction threshold cold cache (123457 tokens)"],
        ["● Compaction threshold cold cache (123457 → 4563, $2.54, 123ms)"],
    ]


async def test_threshold_compaction_displays_cost_saved():
    host = _ConversationHost()
    headers = await host.run(
        [
            _compaction_start("threshold", None),
            _compaction_complete("threshold", estimate=None, cost_saved=0.61),
        ]
    )
    assert headers[-1] == ["● Compaction threshold tokens (123457 → 4563, $0.61, 123ms)"]


async def test_cold_cache_compaction_failure_renders_error():
    host = _ConversationHost()
    headers = await host.run(
        [
            _compaction_start(),
            _compaction_complete(
                status="failure", tokens_after=None, cost_saved=0.0, error="provider [down]"
            ),
        ]
    )
    assert headers[-1] == [
        "● Compaction threshold cold cache (123457 tokens, 123ms) Failed: provider [down]"
    ]


async def test_compaction_status_colours_match_tool_entries():
    from archie_cli.tui import theme
    from archie_cli.tui.conversation import CompactionEntry
    from archie_cli.tui.transcript_view import Conversation
    from textual.widgets import Static

    host = _ConversationHost()
    events = [
        _compaction_start(request_id="ok"),
        _compaction_complete(request_id="ok"),
        _compaction_start("threshold", None, request_id="bad"),
        _compaction_complete(
            "threshold", "interrupted", request_id="bad", tokens_after=None, error=None
        ),
    ]
    controller = host.controller
    async with host.host.run_test() as pilot:
        conv = host.host.query_one(Conversation)
        controller.attach(_ViewListener(controller, conv))
        controller.apply(events[0])
        await pilot.pause()
        pending = conv.query(CompactionEntry).first().query_one(".tool-header", Static)
        assert theme.PRIMARY.lower() in _span_styles(pending)
        for event in events[1:]:
            controller.apply(event)
        await pilot.pause()
        ok, bad = [e.query_one(".tool-header", Static) for e in conv.query(CompactionEntry)]
        assert theme.SUCCESS.lower() in _span_styles(ok)
        assert theme.ERROR.lower() in _span_styles(bad)
        assert str(bad.render()).endswith("Failed: Compaction interrupted")


async def test_unmatched_completion_mounts_terminal_entry():
    host = _ConversationHost()
    headers = await host.run([_compaction_complete()])
    assert headers == [["● Compaction threshold cold cache (123457 → 4563, $2.54, 123ms)"]]


async def test_automatic_compaction_restores_thinking():
    host = _ConversationHost()
    thinking = await host.run(
        [_compaction_start(), _compaction_complete()],
        turn_active=True,
        inspect=lambda _conv: host.controller.thinking,
    )
    assert thinking == [False, True]
    assert host.controller.turn_active is True


@pytest.mark.asyncio
async def test_replay_reset_replaces_the_transcript_model():
    controller, listener = make_controller()
    old = controller.builder
    controller.apply(_user())
    await controller.reset()
    assert controller.builder is not old and controller.builder.turns == []
    assert listener.names()[-2:] == ["reset", "status"]


def test_context_compaction_updates_display_and_ends_manual_turn():
    controller, _ = make_controller()
    controller.turn_active = True
    controller.apply(_user())
    controller.apply(
        ContextCompacted(
            id="c1",
            turn=1,
            iteration=0,
            scope=None,
            request_id="r1",
            reason="manual",
            summary="## Goal\nKeep going",
            model_key="m",
            tokens_before=1000,
            tokens_after=120,
            duration_ms=10,
            debug="counts",
        )
    )
    (marker,) = controller.builder.turns[0].items
    assert marker.kind == "compaction" and marker.marker
    assert (marker.reason, marker.tokens_before, marker.tokens_after) == ("manual", 1000, 120)
    assert marker.summary == "## Goal\nKeep going"
    assert controller.ledger.context_tokens == 120
    assert controller.turn_active is False


def _compacted(reason="threshold", tokens_after=50_000, model_key="bedrock-openai-gpt-6-luna"):
    return ContextCompacted(
        id=f"c-{reason}",
        turn=2,
        iteration=0,
        scope=None,
        request_id="r-compact",
        reason=reason,
        summary="## Goal\nKeep going",
        model_key=model_key,
        tokens_before=150_000,
        tokens_after=tokens_after,
        duration_ms=10,
        debug="counts",
    )


@pytest.mark.asyncio
async def test_replayed_compaction_renders_after_prior_entries_with_session_window():
    """Replay draws the marker in order and sizes context against the session model."""
    conversation_request = _llm_request("l1", 0.1)
    conversation_request.model_key = "bedrock-claude-haiku-4-5"
    client = FakeOrchestrator(
        {"s1": [_user("hi"), conversation_request, _compacted(tokens_after=50_000)]}
    )
    controller, _ = make_controller(client)

    assert await controller.replay() is True

    (turn,) = controller.builder.turns
    assert turn.user.content == "hi"
    assert [item.kind for item in turn.items] == ["compaction"]
    assert controller.ledger.context_tokens == 50_000
    # Haiku's 200k window, not the compaction model's 1M window.
    assert controller.ledger.context_pct == pytest.approx(25.0)


def test_automatic_compaction_does_not_end_turn():
    controller, _ = make_controller()
    controller.turn_active = True
    controller.apply(_compacted(reason="cold_cache"))
    controller.apply(ErrorNotice(id="e-auto", kind="compaction_failed", message="failed"))
    assert controller.turn_active is True


def test_tool_policy_notices_do_not_end_turn():
    controller, _ = make_controller()
    controller.turn_active = True
    for kind in ("tool_policy_reload_failed", "tool_policy_reloaded"):
        controller.apply(ErrorNotice(id=f"e-{kind}", kind=kind, message="policy"))
    assert controller.turn_active is True
    assert local_texts(controller) == ["policy", "policy"]


def test_manual_compaction_failure_ends_pending_turn():
    controller, _ = make_controller()
    controller.turn_active = True
    controller.input_locked = True
    controller.apply(ErrorNotice(id="e-manual", kind="manual_compaction_failed", message="failed"))
    assert controller.turn_active is False
    assert controller.input_locked is False


def test_turn_error_notice_ends_pending_turn():
    """A server-side rejection/error returns the session to an idle state."""
    controller, listener = make_controller()
    controller.turn_active = True
    controller.thinking = True
    controller.input_locked = True

    controller.apply(ErrorNotice(id="e1", kind="turn_error", message="provider failed"))

    assert controller.turn_active is False
    assert controller.thinking is False
    assert controller.input_locked is False
    assert controller.turns_ended == 1
    assert local_texts(controller) == ["provider failed"]
    assert listener.names()[-2:] == ["local", "turn"]


def test_content_filtered_turn_shows_compact_and_rephrase_guidance():
    """A content-filtered root turn renders an error telling the user to /compact and rephrase."""
    from archie_shared.transcript import CONTENT_FILTERED_MESSAGE

    controller, _ = make_controller()
    controller.apply(_user())
    controller.turn_active = True
    controller.input_locked = True

    controller.apply(_root_turn_complete("content_filtered"))

    (notice,) = controller.builder.turns[0].items
    assert notice.variant == "turn-error" and notice.detail == CONTENT_FILTERED_MESSAGE
    assert "/compact" in CONTENT_FILTERED_MESSAGE
    assert "Rephrase" in CONTENT_FILTERED_MESSAGE
    assert controller.turn_active is False
    assert controller.input_locked is False


def test_end_turn_does_not_show_error():
    controller, _ = make_controller()
    controller.apply(_root_turn_complete("end_turn"))
    assert not any(item.kind == "notice" for item in controller.builder.preamble)


def test_render_canonical_does_not_create_empty_iteration_block():
    """Text-only replay iterations do not leave an empty visual block."""
    controller, _ = make_controller()
    controller.apply(_user())
    controller.apply(IterationStart(id="i1", turn=1, iteration=1, scope=None))
    assert controller.builder.turns[0].items == []


def _tool_call(tool_use_id, name, tool_input, iteration=1):
    return ToolCall(
        id=f"c-{tool_use_id}",
        turn=1,
        iteration=iteration,
        scope=None,
        request_id="r1",
        tool_use_id=tool_use_id,
        name=name,
        input=tool_input,
    )


def _tool_result(tool_use_id, content, *, is_error=False, iteration=1, duration_ms=5):
    return ToolResult(
        id=f"r-{tool_use_id}",
        turn=1,
        iteration=iteration,
        scope=None,
        request_id="r1",
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
        duration_ms=duration_ms,
        result_bytes=len(content),
        result_lines=len(content.splitlines()),
    )


def _tool_rows(conv):
    from archie_cli.tui.transcript_view import IterationBlock, ToolRow

    return [
        [str(row.render()) for row in block.query(ToolRow)] for block in conv.query(IterationBlock)
    ]


async def test_tool_rows_render_pending_then_complete_summaries():
    """Rows are the shared two-line model: running, then finished (tool_rows)."""
    host = _ConversationHost()
    snapshots = await host.run(
        [
            _user(),
            IterationStart(id="i1", turn=1, iteration=1, scope=None),
            _tool_call("t1", "read", {"path": "/etc/hosts"}),
            _tool_result("t1", "File: /etc/hosts (3 lines)\n"),
        ],
        inspect=_tool_rows,
    )
    assert snapshots[1] == []
    assert snapshots[2] == [["⠋ Read\n╰ /etc/hosts"]]
    assert snapshots[3] == [["● Read · 3 lines · 5ms\n╰ /etc/hosts"]]


async def test_parallel_tools_share_one_block_and_iterations_split():
    host = _ConversationHost()
    snapshots = await host.run(
        [
            _user(),
            _tool_call("t1", "read", {"path": "a"}),
            _tool_call("t2", "read", {"path": "b"}),
            _tool_call("t3", "read", {"path": "c"}, iteration=2),
        ],
        inspect=_tool_rows,
    )
    blocks = snapshots[-1]
    assert [len(b) for b in blocks] == [2, 1]


async def test_replies_and_iteration_blocks_have_transparent_rules_and_user_messages_a_bar():
    from pathlib import Path

    import archie_cli.tui.app as tui_app
    from archie_cli.tui import theme
    from archie_cli.tui.conversation import AssistantMessage as AssistantRow
    from archie_cli.tui.conversation import UserMessage as UserRow
    from archie_cli.tui.transcript_view import Conversation, IterationBlock
    from textual.app import App

    class Host(App):
        CSS_PATH = Path(tui_app.__file__).parent / "archie.tcss"

        def __init__(self):
            super().__init__()
            self.register_theme(theme.THEME)
            self.theme = "archie"

        def compose(self):
            yield Conversation(id="conversation")

    host = Host()
    builder = TranscriptBuilder()
    async with host.run_test() as pilot:
        conv = host.query_one(Conversation)
        conv.apply_changes(builder, builder.apply(_user()))
        conv.apply_changes(builder, builder.apply(_assistant(content="looking")))
        conv.apply_changes(builder, builder.apply(_tool_call("t1", "read", {"path": "a"})))
        await pilot.pause()
        kind, colour = conv.query_one(AssistantRow).styles.border_left
        assert kind == "thick" and colour.a == 0  # lines the text up with the tool rows
        kind, colour = conv.query_one(UserRow).styles.border_left
        assert kind == "thick" and colour.a == 1
        block = conv.query_one(IterationBlock)
        assert block.styles.border_left[1].a == 0
        assert not block.focusable


async def test_exec_and_shell_rows_are_two_lines():
    source = "\n".join(f"x{i} = {i}" for i in range(25))
    command = "\n".join(f"echo {i}" for i in range(14))
    host = _ConversationHost()
    snapshots = await host.run(
        [
            _user(),
            _tool_call("e1", "exec", {"source": source}),
            _tool_call("s1", "shell", {"command": command}),
        ],
        inspect=_tool_rows,
    )
    # The source and the whole command are in the detail view, not the row.
    exec_row, shell_row = snapshots[-1][0]
    assert exec_row == "⠋ Exec\n╰ python · 25 lines"
    assert shell_row == "⠋ Shell\n╰ $ echo 0"


# --- Replay -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_dispatches_through_apply():
    """Stored events use the same application entry point as live events."""
    event = _user("hi")
    controller, _ = make_controller(FakeOrchestrator({"s1": [event]}))

    with patch.object(controller, "apply") as apply:
        assert await controller.replay() is True

    apply.assert_called_once_with(event)


@pytest.mark.asyncio
async def test_replay_events_seeds_cost_from_ledger():
    """A full replay containing an llm_request folds its cost into accounting."""
    client = FakeOrchestrator(
        {"s1": [_user("hi"), _llm_request("l1", 0.25, input_tokens=200, output_tokens=20)]}
    )
    client.logs["s1"].append(_assistant(request_id="l1"))
    controller, listener = make_controller(client)

    await controller.replay()

    assert client.requests == [("s1", None)]
    assert controller.ledger.cost == pytest.approx(0.25)
    assert controller.ledger.input_tokens == 200
    assert controller.cursor == "a1"
    replay = [call for call in listener.calls if call[0] == "replay"]
    assert replay == [("replay", True), ("replay", False)]


@pytest.mark.asyncio
async def test_replay_events_409_falls_back_to_full_replay():
    """An unknown cursor (409) resets state, tells the listener and refetches the log."""
    log = [_user("hi"), _llm_request("l1", 0.25, input_tokens=200, output_tokens=20)]
    client = FakeOrchestrator({"s1": list(log)})
    controller, listener = make_controller(client)
    for event in log:  # already applied, so the reload must count ``l1`` again
        controller.apply(event)
    controller.apply(_llm_request("gone", 9.99, input_tokens=999))  # not in the log
    assert controller.cursor == "gone"

    assert await controller.replay() is True

    assert client.requests == [("s1", "gone"), ("s1", None)]  # then the full log
    assert listener.names().count("reset") == 1
    assert listener.names().index("reset") < listener.calls.index(("replay", True))
    assert [turn.user.content for turn in controller.builder.turns] == ["hi"]
    assert controller.cursor == "l1"
    # Accounting restarted from the log: ``gone`` is dropped, ``l1`` counted once.
    assert controller.ledger.input_tokens == 200
    assert controller.ledger.cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_scripted_409_once_resets_then_reads_the_log():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.reject_cursor_once()
    controller, listener = make_controller(client)

    assert await controller.replay() is True
    assert listener.names().count("reset") == 1
    assert [turn.user.content for turn in controller.builder.turns] == ["hi"]


@pytest.mark.asyncio
async def test_replay_events_reports_http_failure():
    """Replay failure is explicit instead of being treated as an empty history."""
    client = FakeOrchestrator()
    client.reply(OrchestratorError("history request failed: 503", 503))
    controller, listener = make_controller(client)
    assert await controller.replay() is False
    assert "replay" not in listener.names()


@pytest.mark.asyncio
async def test_replay_yields_to_the_event_loop_every_200_events():
    events = [_user(f"q{n}", event_id=f"u{n}") for n in range(450)]
    controller, _ = make_controller(FakeOrchestrator({"s1": events}))
    sleeps = []

    async def record(delay):
        sleeps.append(delay)

    with patch("archie_cli.tui.session.asyncio.sleep", record):
        assert await controller.replay() is True
    # One yield in the fake request, then one after events 200 and 400.
    assert sleeps == [0, 0, 0]
    assert len(controller.builder.turns) == 450


@pytest.mark.asyncio
async def test_http_orchestrator_maps_event_statuses():
    def http(status_code, text=""):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=MagicMock(status_code=status_code, text=text))
        return client

    orchestrator = HttpOrchestrator("http://127.0.0.1:7600/")
    ok = http(200, "body")
    with patch("archie_cli.tui.client.httpx.AsyncClient", return_value=ok):
        assert await orchestrator.events("s1", "e3") == "body"
    ok.get.assert_awaited_once_with(
        "http://127.0.0.1:7600/api/sessions/s1/events?after=e3", timeout=5.0
    )
    with (
        patch("archie_cli.tui.client.httpx.AsyncClient", return_value=http(409)),
        pytest.raises(CursorNotFound),
    ):
        await orchestrator.events("s1", "gone")
    with (
        patch("archie_cli.tui.client.httpx.AsyncClient", return_value=http(503)),
        pytest.raises(OrchestratorError) as raised,
    ):
        await orchestrator.events("s1", None)
    assert raised.value.status_code == 503
    assert orchestrator.stream_url("s1") == "ws://127.0.0.1:7600/api/sessions/s1/stream"
    assert HttpOrchestrator("https://h").stream_url("s1") == "wss://h/api/sessions/s1/stream"


# --- Connection ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_replay_reports_loaded_once():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    controller, listener = make_controller(client)

    await controller.start()

    assert listener.names().count("loaded") == 1
    assert listener.names().index("loaded") > listener.calls.index(("replay", False))
    assert controller.loaded

    controller.RECONNECT_DELAYS = (0.0,)
    client.latest.drop()
    for _ in range(20):
        if controller.reconnecting:
            break
        await asyncio.sleep(0)
    assert await controller.wait_connected() is True
    assert len(client.streams) == 2
    assert listener.names().count("loaded") == 1  # later replays do not repeat it
    await controller.close()


@pytest.mark.asyncio
async def test_loaded_waits_for_the_first_replay_that_lands():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.reply(OrchestratorError("down"))
    controller, listener = make_controller(client)
    controller.RECONNECT_DELAYS = (0.0,)

    await controller.start()
    assert "loaded" not in listener.names()
    assert local_texts(controller) == ["Initial event replay failed; retrying connection."]

    assert await controller.wait_connected() is True
    assert listener.names().count("loaded") == 1
    assert [turn.user.content for turn in controller.builder.turns] == ["hi"]
    await controller.close()


@pytest.mark.asyncio
async def test_first_connect_failure_is_retried_as_an_outage():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.fail_connects(OSError("offline"))
    controller, _ = make_controller(client)
    controller.RECONNECT_DELAYS = (0.0,)

    await controller.start()
    assert local_texts(controller) == [] and client.requests == []
    assert controller.reconnecting

    assert await controller.wait_connected() is True
    assert local_texts(controller) == [RECONNECTING_TEXT]
    assert controller.loaded and [t.user.content for t in controller.builder.turns] == ["hi"]
    await controller.close()


async def _until(predicate, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate(), "condition not reached"


@pytest.mark.asyncio
async def test_ending_during_the_first_load_keeps_the_live_frames_it_buffered():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.events_gate = asyncio.Event()
    controller, _ = make_controller(client)
    start = asyncio.create_task(controller.start())
    await _until(lambda: client.requests)  # the body is read; the request waits

    # A frame the stream delivers meanwhile; it is in the log, but after the body.
    late = _assistant("a1", "late")
    stream = client.latest
    stream.push(late)
    client.logs["s1"].append(late)
    await _until(lambda: stream._frames.empty())  # received while the load runs
    controller.end()
    await _until(lambda: not stream.connected)  # the stream closes at once
    client.events_gate.set()
    await start
    await _until(lambda: len(client.requests) == 2)

    (turn,) = controller.builder.turns
    assert [getattr(item, "content", None) for item in turn.items] == ["late"]  # once
    assert client.requests == [("s1", None), ("s1", "a1")]  # then the tail after it
    assert controller.loaded and not controller.live
    await controller.close()


@pytest.mark.asyncio
async def test_ending_while_the_first_connect_waits_loads_the_log_without_a_stream():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.stream_gate = asyncio.Event()
    controller, _ = make_controller(client)
    start = controller.start_soon()
    await _until(lambda: client.stream_calls == 1)
    controller.end()
    await asyncio.sleep(0)
    client.stream_gate.set()
    await start
    await _until(lambda: controller.loaded)

    assert [turn.user.content for turn in controller.builder.turns] == ["hi"]
    assert client.full_loads("s1") == 1
    assert not any(stream.connected for stream in client.streams)
    await controller.close()


@pytest.mark.asyncio
async def test_resume_and_force_reconnect_open_no_second_stream_while_starting():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.stream_gate = asyncio.Event()
    controller, _ = make_controller(client)
    start = controller.start_soon()  # as the app starts it: in the background
    controller.resume()  # a feed snapshot before ``start`` ran
    await _until(lambda: client.stream_calls == 1)
    controller.resume()  # and one while the stream opens
    controller.force_reconnect()
    assert controller.connecting and not controller.reconnecting
    client.stream_gate.set()
    await start
    assert await controller.wait_connected() is True

    assert client.stream_calls == 1 and client.opened("s1") == 1
    assert client.full_loads("s1") == 1
    await controller.close()
    assert not any(stream.connected for stream in client.streams)


@pytest.mark.asyncio
async def test_a_stream_lost_during_the_first_load_reconnects_once_it_lands():
    client = FakeOrchestrator({"s1": [_user("hi")]})
    client.events_gate = asyncio.Event()
    controller, _ = make_controller(client)
    controller.RECONNECT_DELAYS = (0.0,)
    start = asyncio.create_task(controller.start())
    await _until(lambda: client.requests)
    client.latest.drop()
    await asyncio.sleep(0)
    assert not controller.reconnecting  # deferred while ``start`` loads
    client.events_gate.set()
    await start
    assert await controller.wait_connected() is True
    assert client.opened("s1") == 2 and controller.loaded
    await controller.close()


@pytest.mark.asyncio
async def test_a_reconnect_closes_a_stream_it_replaces():
    client = FakeOrchestrator()
    controller, _ = make_controller(client)
    await controller.start()
    first = client.latest
    controller.RECONNECT_DELAYS = (0.0,)
    # A failed send asks for a reconnect while the old stream still reports connected.
    first.send_message.side_effect = RuntimeError("socket closed")
    controller.submit("hello")
    await _until(lambda: client.opened("s1") == 2)
    assert await controller.wait_connected() is True
    first.disconnect.assert_awaited()
    assert controller.ws is client.latest and not first.connected
    await controller.close()


@pytest.mark.asyncio
async def test_reconnect_replays_after_cursor_and_deduplicates_buffered_live():
    """Reconnect reconciliation keeps the completed turn and ledger totals stable."""
    ledger = _llm_request("l1", 0.25, input_tokens=200, output_tokens=20)
    client = FakeOrchestrator({"s1": [ledger]})
    controller, _ = make_controller(client)
    controller.apply(ledger)
    controller.RECONNECT_DELAYS = (0.0,)
    # Live frames that arrive while the replay request is in flight are buffered.
    client.frames_on_connect(
        Handshake(id="h1", protocol_version=3, session_id="session-1"),
        SessionStatus(id="s1", model_key="m", git_branch="main"),
        ledger,
    )

    assert await controller.ensure_connected() is True

    assert client.requests == [("s1", "l1")]
    assert controller.agent_protocol == 3
    assert controller.model_key == "m"
    assert controller.cursor == "l1"
    assert controller.ledger.input_tokens == 200
    assert controller.ledger.cost == pytest.approx(0.25)
    await controller.close()


@pytest.mark.asyncio
async def test_reconnect_retries_when_replay_fails():
    """Reconnect does not report success until canonical replay succeeds."""
    client = FakeOrchestrator()
    client.reply(OrchestratorError("down"), "")
    controller, listener = make_controller(client)
    controller.RECONNECT_DELAYS = (0.0, 0.0)

    assert await controller.ensure_connected() is True

    assert len(client.streams) == 2
    assert len(client.requests) == 2
    client.streams[0].disconnect.assert_awaited_once()
    # One entry for the whole outage, however many attempts it took.
    assert local_texts(controller) == [RECONNECTING_TEXT]
    assert [entry.message for (entry,) in listener.of("local")] == [RECONNECTING_TEXT]
    await controller.close()


@pytest.mark.asyncio
async def test_receive_disconnect_preserves_partial_stream_until_replay():
    """A transient socket close must not finalize partial assistant output."""
    controller, _ = make_controller()
    await controller.start()
    controller.apply(_user())
    controller.turn_active = True
    controller.apply(
        TextDelta(id="d1", turn=1, iteration=1, scope=None, request_id="r1", text="partial")
    )

    with patch.object(controller, "_schedule_reconnect") as schedule:
        controller.ws.close()
        await controller._receive_task

    schedule.assert_called_once_with()
    assert local_texts(controller) == []  # the outage entry waits for the first back-off
    assert controller.turn_active is True
    (item,) = controller.builder.turns[0].items
    assert item.kind == "stream" and item.text == "partial"


# --- Children -----------------------------------------------------------------------


def _child_texts(controller, index=0):
    sub = controller.builder.subagents[("task-1", index)]
    return [(item.kind, getattr(item, "content", getattr(item, "text", ""))) for item in sub.items]


def _child_assistant(event_id, content, *, index=0, iteration=1, request_id="request-1"):
    return AssistantMessage(
        id=event_id,
        turn=1,
        scope="task-1",
        subagent_index=index,
        iteration=iteration,
        request_id=request_id,
        content=content,
        interrupted=False,
    )


def _child_delta(event_id, text, *, index=0, iteration=1, request_id="request-1"):
    return TextDelta(
        id=event_id,
        turn=1,
        iteration=iteration,
        scope="task-1",
        subagent_index=index,
        request_id=request_id,
        text=text,
    )


def test_replay_child_assistant_message_reconstructs_child_output():
    """Persisted child assistant messages rebuild the child transcript on replay."""
    controller, _ = make_controller()
    controller.apply(_child_assistant("child-assistant", "persisted child response"))
    assert _child_texts(controller) == [("text", "persisted child response")]


def test_child_replay_replaces_live_child_deltas():
    """Durable child content replaces partial live deltas after reconnect."""
    controller, _ = make_controller()
    controller.apply(_child_delta("child-delta", "partial "))
    controller.apply(_child_assistant("child-assistant-reconnect", "partial response"))
    assert _child_texts(controller) == [("text", "partial response")]


def test_child_replay_preserves_durable_lines_across_requests():
    """Reconnect reconciliation keeps each request's durable child answer."""
    controller, _ = make_controller()
    controller.apply(_child_assistant("child-assistant-1", "first answer"))
    controller.apply(
        _child_delta("child-delta-2", "second partial", iteration=2, request_id="request-2")
    )
    controller.apply(
        _child_assistant("child-assistant-2", "second answer", iteration=2, request_id="request-2")
    )
    assert _child_texts(controller) == [("text", "first answer"), ("text", "second answer")]


def test_sibling_child_streams_finalize_independently():
    """Finalizing one child request leaves a sibling transient request untouched."""
    controller, _ = make_controller()
    controller.apply(_child_delta("child-zero-delta", "zero partial", request_id="request-zero"))
    controller.apply(
        _child_delta("child-one-delta", "one partial", index=1, request_id="request-one")
    )
    controller.apply(
        _child_assistant("child-zero-assistant", "zero answer", request_id="request-zero")
    )

    assert _child_texts(controller, 0) == [("text", "zero answer")]
    assert _child_texts(controller, 1) == [("stream", "one partial")]
    assert controller.builder.subagents[("task-1", 1)].activity == "Responding..."


@pytest.mark.asyncio
@pytest.mark.parametrize("scope, subagent_index", [(None, None), ("task-1", 0)])
async def test_replay_assistant_suppresses_matching_buffered_delta(scope, subagent_index):
    """Durable assistant content wins over a duplicate live delta buffered during replay."""
    assistant = AssistantMessage(
        id=f"assistant-{scope}",
        turn=1,
        scope=scope,
        subagent_index=subagent_index,
        iteration=1,
        request_id="request-1",
        content="durable response",
        interrupted=False,
    )
    delta = TextDelta(
        id=f"delta-{scope}",
        turn=1,
        iteration=1,
        scope=scope,
        subagent_index=subagent_index,
        request_id="request-1",
        text="durable response",
    )
    log = [_user(), assistant] if scope is None else [assistant]
    client = FakeOrchestrator({"s1": log})
    client.frames_on_connect(delta)
    controller, listener = make_controller(client)

    await controller.start()

    items = (
        controller.builder.turns[0].items
        if scope is None
        else controller.builder.subagents[(scope, subagent_index)].items
    )
    assert [(i.kind, i.content) for i in items] == [("text", "durable response")]
    assert listener.names()[-1] == "loaded"  # the late delta changed nothing
    await controller.close()


# --- Local entries ----------------------------------------------------------------


def test_local_entries_record_the_last_root_row_as_anchor():
    controller, listener = make_controller()
    controller.client_error("before anything")
    controller.apply(_user())
    controller.client_error("after the user message")
    controller.apply(_assistant())
    controller.apply(_llm_request("l1", 0.01))
    controller.apply(_root_turn_complete("end_turn"))

    turn = controller.builder.turns[0]
    first, second, status = controller.locals
    assert first == ClientErrorEntry(None, "before anything")
    assert second.anchor == turn.user.id
    assert isinstance(status, TurnStatusEntry) and status.anchor == turn.items[-1].id
    assert [entry for (entry,) in listener.of("local")] == controller.locals


@pytest.mark.asyncio
async def test_local_entries_survive_a_reset():
    controller, _ = make_controller()
    controller.apply(_user())
    controller.client_error("kept")
    await controller.reset()
    assert local_texts(controller) == ["kept"]


def test_local_entry_before_any_turn_anchors_to_the_preamble():
    controller, _ = make_controller()
    controller.apply(_assistant("pre", "before any user message"))
    controller.client_error("x")
    assert controller.locals[-1].anchor == controller.builder.preamble[-1].id


# --- Direct shell -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_shell_renders_locally_without_network_logging():
    """The shell output is a local entry; nothing reaches the orchestrator."""
    client = FakeOrchestrator()
    controller, listener = make_controller(client)
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"output\n", None))

    with patch(
        "archie_cli.tui.session.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ) as exec_:
        await controller.run_shell("printf output")

    assert exec_.await_args.args[:5] == ("docker", "exec", "-w", "/workspace", "archie-s1")
    assert controller.locals == [ShellOutputEntry(None, "printf output", "output\n", 0)]
    assert listener.of("local") == [(controller.locals[0],)]
    assert client.requests == [] and client.streams == []
    assert not controller.shell_active


@pytest.mark.asyncio
async def test_direct_shell_cancel_renders_local_output():
    """Esc cancels local shell execution without creating a session event."""
    controller, _ = make_controller()
    released = asyncio.Event()

    class FakeProcess:
        returncode = None

        def kill(self):
            self.returncode = -9
            released.set()

        async def communicate(self):
            await released.wait()
            return b"partial\n", None

    process = FakeProcess()
    with patch(
        "archie_cli.tui.session.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ):
        task = asyncio.create_task(controller.run_shell("long command"))
        while controller._shell_proc is not process:
            await asyncio.sleep(0)
        assert controller.cancel() == "shell"
        await task

    assert controller.locals == [ShellOutputEntry(None, "long command", "partial\n", 130)]


@pytest.mark.asyncio
async def test_second_shell_command_while_one_runs_is_refused():
    controller, _ = make_controller()
    controller.shell_active = True
    controller.submit("!ls")
    assert local_texts(controller) == ["Shell command already running"]


@pytest.mark.asyncio
async def test_direct_shell_timeout_renders_exit_124():
    controller, _ = make_controller()
    process = MagicMock(returncode=None)
    process.communicate = MagicMock()
    process.wait = AsyncMock()

    with (
        patch(
            "archie_cli.tui.session.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("archie_cli.tui.session.asyncio.wait_for", side_effect=TimeoutError),
    ):
        await controller.run_shell("sleep 60")

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once()
    assert controller.locals == [ShellOutputEntry(None, "sleep 60", "(timed out)", 124)]


@pytest.mark.asyncio
async def test_direct_shell_truncates_output_after_10000_lines():
    controller, _ = make_controller()
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(
        return_value=("\n".join(f"line-{index}" for index in range(10_001)).encode(), None)
    )

    with patch(
        "archie_cli.tui.session.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ):
        await controller.run_shell("generate-lines")

    output = controller.locals[0].output
    assert output.splitlines()[-1] == "(truncated)"
    assert len(output.splitlines()) == 10_001
    assert output.splitlines()[0] == "line-0"
    assert output.splitlines()[-2] == "line-9999"


@pytest.mark.asyncio
async def test_direct_shell_failure_renders_local_error():
    controller, _ = make_controller()

    with patch(
        "archie_cli.tui.session.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        side_effect=OSError("docker unavailable"),
    ):
        await controller.run_shell("pwd")

    assert controller.locals == [ClientErrorEntry(None, "Docker exec failed: docker unavailable")]
