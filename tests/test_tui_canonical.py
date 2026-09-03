"""TUI-side canonical event handling: ledger accounting, reducer, replay.

These exercise the M4 client-side reconstruction path without a running
Textual app by stubbing `query_one` and the status-bar update.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_shared.events import (
    AssistantMessage,
    ErrorNotice,
    Handshake,
    IterationStart,
    LLMRequest,
    SessionStatus,
    TextDelta,
    ToolCall,
    ToolResult,
    UserMessage,
    encode_event,
)


def _make_app():
    from archie_cli.tui.app import ArchieApp

    return ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/sessions/proj-01abc",
        container_name="archie-proj-01abc",
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


def test_accumulate_ledger_folds_cost_and_tokens():
    app = _make_app()
    with patch.object(app, "_update_accounting_status"):
        app._accumulate_ledger(_llm_request("e1", 0.10, input_tokens=100, output_tokens=10))
        app._accumulate_ledger(_llm_request("e2", 0.05, input_tokens=50, output_tokens=5))

    assert app._cumulative_cost == pytest.approx(0.15)
    assert app._cumulative_input == 150
    assert app._cumulative_output == 15


def test_child_ledger_does_not_overwrite_root_context():
    app = _make_app()
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
    with patch.object(app, "_update_accounting_status"):
        app._accumulate_ledger(root)
        app._accumulate_ledger(child)

    assert app._latest_context_tokens == 1010
    assert app._cumulative_input == 10000
    assert app._cumulative_cost == pytest.approx(0.03)


def test_accumulate_ledger_deduplicates_by_id():
    """Replay + live broadcast of the same llm_request must not double-count."""
    app = _make_app()
    with patch.object(app, "_update_accounting_status"):
        app._accumulate_ledger(_llm_request("e1", 0.10))
        app._accumulate_ledger(_llm_request("e1", 0.10))  # duplicate id

    assert app._cumulative_cost == pytest.approx(0.10)
    assert app._cumulative_input == 100


def test_accumulate_ledger_resets_streaming_output_estimate():
    app = _make_app()
    app._estimated_output = 25
    with patch.object(app, "_update_accounting_status"):
        app._accumulate_ledger(_llm_request("e1", 0.10))

    assert app._estimated_output == 0


def test_live_assistant_message_finalizes_stream_without_duplicate():
    """The persisted assistant record must reconcile the live text stream."""
    app = _make_app()
    conv = MagicMock()
    text_delta = TextDelta(
        id="d1",
        turn=1,
        iteration=1,
        scope=None,
        request_id="r1",
        text="hello",
    )
    assistant = AssistantMessage(
        id="a1",
        turn=1,
        scope=None,
        iteration=1,
        request_id="r1",
        content="hello",
        interrupted=False,
    )

    with patch.object(app, "query_one", return_value=conv):
        app._apply_event(text_delta)
        app._apply_event(assistant)

    conv.finalise_streaming.assert_called_once()
    conv.add_assistant_message.assert_not_called()


def test_throbber_tracks_provider_request_lifecycle():
    """Show while a provider request is pending, not while tools execute."""
    app = _make_app()
    app._turn_active = True
    throbber = MagicMock()
    conv = MagicMock()
    conv.begin_streaming.return_value = MagicMock()
    conv.begin_iteration.return_value = MagicMock()
    status = MagicMock()

    def query_one(selector, _type=None):
        return {"#conversation": conv, "#throbber": throbber, "#status": status}[selector]

    with patch.object(app, "query_one", side_effect=query_one):
        app._apply_event(IterationStart(id="i1", turn=1, iteration=0, scope=None))
        assert throbber.display is True

        app._apply_event(
            TextDelta(
                id="d1",
                turn=1,
                iteration=0,
                scope=None,
                request_id="r1",
                text="partial",
            )
        )
        assert throbber.display is False

        app._apply_event(IterationStart(id="i2", turn=1, iteration=1, scope=None))
        assert throbber.display is True
        app._apply_event(
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
        assert throbber.display is False


def test_turn_error_notice_ends_pending_turn():
    """A server-side rejection/error returns the local TUI to an idle state."""
    app = _make_app()
    app._turn_active = True
    throbber = MagicMock()
    conv = MagicMock()
    input_widget = MagicMock()
    app._throbber = throbber

    def query_one(selector, _type=None):
        return {"#conversation": conv, "#throbber": throbber, "#input": input_widget}[selector]

    with patch.object(app, "query_one", side_effect=query_one):
        app._apply_event(ErrorNotice(id="e1", kind="turn_error", message="provider failed"))

    assert app._turn_active is False
    assert input_widget.disabled is False
    conv.add_client_error.assert_called_once_with("provider failed")


def test_render_canonical_does_not_create_empty_iteration_block():
    """Text-only replay iterations do not leave an empty visual block."""
    app = _make_app()
    conv = MagicMock()
    with patch.object(app, "query_one", return_value=conv):
        app._apply_event(IterationStart(id="i1", turn=1, iteration=1, scope=None))

    conv.begin_iteration.assert_not_called()


def test_render_canonical_reconstructs_tool_summary_client_side():
    """ToolCall input is cached so ToolResult renders a shared-formatter summary."""
    app = _make_app()
    block = MagicMock()
    conv = MagicMock()
    conv.begin_iteration.return_value = block
    with patch.object(app, "query_one", return_value=conv):
        app._apply_event(IterationStart(id="i1", turn=1, iteration=1, scope=None))
        app._apply_event(
            ToolCall(
                id="c1",
                turn=1,
                iteration=1,
                scope=None,
                request_id="r1",
                tool_use_id="t1",
                name="read",
                input={"path": "/etc/hosts"},
            )
        )
        app._apply_event(
            ToolResult(
                id="r1e",
                turn=1,
                iteration=1,
                scope=None,
                request_id="r1",
                tool_use_id="t1",
                content="file contents",
                is_error=False,
                duration_ms=5,
                result_bytes=13,
            )
        )

    # pending input was cached then consumed
    assert "t1" not in app._pending_tool_inputs
    block.add_pending.assert_called_once()
    pending_args = block.add_pending.call_args.args
    assert pending_args[0] == "t1"
    assert pending_args[1] == "read"
    # summary derived from raw input, not empty
    assert "/etc/hosts" in pending_args[2]

    block.complete_tool.assert_called_once()
    complete_args = block.complete_tool.call_args.args
    assert complete_args[0] == "t1"
    assert complete_args[1] is False  # is_error
    # summary string is non-empty (reconstructed via shared formatter)
    assert complete_args[4]


@pytest.mark.asyncio
async def test_replay_dispatches_through_apply_event_as_historical(tmp_path):
    """Stored events use the same public application entry point as live events."""
    app = _make_app()
    event = UserMessage(id="u1", turn=1, scope=None, content="hi")
    response = MagicMock(status_code=200, text=encode_event(event) + "\n")
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(return_value=response)

    with (
        patch("archie_cli.tui.app.httpx.AsyncClient", return_value=mock_http),
        patch.object(app, "_apply_event") as apply_event,
    ):
        assert await app._replay_events() is True

    apply_event.assert_called_once_with(event, historical=True)


@pytest.mark.asyncio
async def test_replay_events_seeds_cost_from_ledger():
    """A full replay containing an llm_request folds its cost into accounting."""
    app = _make_app()
    events = [
        UserMessage(id="u1", turn=1, scope=None, content="hi"),
        _llm_request("l1", 0.25, input_tokens=200, output_tokens=20),
        AssistantMessage(
            id="a1",
            turn=1,
            scope=None,
            iteration=1,
            request_id="l1",
            content="ok",
            interrupted=False,
        ),
    ]
    body = "".join(encode_event(e) + "\n" for e in events)
    resp = MagicMock(status_code=200, text=body)

    conv = MagicMock()
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(return_value=resp)

    with (
        patch("archie_cli.tui.app.httpx.AsyncClient", return_value=mock_http),
        patch.object(app, "query_one", return_value=conv),
        patch.object(app, "_update_accounting_status"),
    ):
        await app._replay_events()

    assert app._cumulative_cost == pytest.approx(0.25)
    assert app._cumulative_input == 200
    assert app._last_event_id == "a1"


@pytest.mark.asyncio
async def test_replay_events_409_falls_back_to_full_replay():
    """An unknown cursor (409) clears state and re-requests the full log."""
    app = _make_app()
    app._last_event_id = "gone"
    app._seen_event_ids = {"gone"}
    app._seen_accounted_ids = {"old-ledger"}
    app._cumulative_input = 999
    app._cumulative_cost = 9.99

    full_events = [
        UserMessage(id="u1", turn=1, scope=None, content="hi"),
        _llm_request("l1", 0.25, input_tokens=200, output_tokens=20),
    ]
    full_body = "".join(encode_event(e) + "\n" for e in full_events)
    resp_409 = MagicMock(status_code=409, text="")
    resp_full = MagicMock(status_code=200, text=full_body)

    urls = []

    async def _get(url, **kwargs):
        urls.append(url)
        return resp_409 if len(urls) == 1 else resp_full

    conv = MagicMock()
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = _get

    with (
        patch("archie_cli.tui.app.httpx.AsyncClient", return_value=mock_http),
        patch.object(app, "query_one", return_value=conv),
    ):
        await app._replay_events()

    assert "after=gone" in urls[0]
    assert urls[1].endswith("/events")  # full replay, no cursor
    conv.add_user_message.assert_called_once_with("hi")
    conv.remove_children.assert_called_once_with()
    assert app._last_event_id == "l1"
    assert app._seen_accounted_ids == {"l1"}
    assert app._cumulative_input == 200
    assert app._cumulative_cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_reconnect_replays_after_cursor_and_deduplicates_buffered_live():
    """Reconnect reconciliation keeps the completed turn and ledger totals stable."""
    app = _make_app()
    ledger = _llm_request("l1", 0.25, input_tokens=200, output_tokens=20)
    with patch.object(app, "_update_accounting_status"):
        app._accumulate_ledger(ledger)
    app._last_event_id = ledger.id
    app._seen_event_ids.add(ledger.id)

    status = MagicMock()
    conversation = MagicMock()

    def query_one(selector, _type=None):
        return {"#status": status, "#conversation": conversation}[selector]

    response = MagicMock(status_code=200, text="")
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)

    async def get_response(*args, **kwargs):
        await asyncio.sleep(0)
        return response

    mock_http.get = AsyncMock(side_effect=get_response)

    async def buffered_receive_loop():
        app._event_buffer.extend(
            [
                Handshake(
                    id="h1",
                    protocol_version=2,
                    session_id="session-1",
                ),
                SessionStatus(id="s1", model_key="m", git_branch="main"),
                ledger,
            ]
        )

    app._ws.connect = AsyncMock()
    app._receive_loop = buffered_receive_loop

    with (
        patch("archie_cli.tui.app.httpx.AsyncClient", return_value=mock_http),
        patch.object(app, "query_one", side_effect=query_one),
        patch.object(app, "notify"),
    ):
        await app._reconnect()

    app._ws.connect.assert_awaited_once_with(app._ws_url)
    requested_url = mock_http.get.await_args.args[0]
    assert requested_url.endswith(f"/events?after={ledger.id}")
    assert app._session_id == "session-1"
    assert status.git_branch == "main"
    assert app._last_event_id == ledger.id
    assert app._seen_accounted_ids == {ledger.id}
    assert app._cumulative_input == 200
    assert app._cumulative_cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_direct_shell_renders_locally_without_network_logging():
    """The initiating TUI renders shell output directly and makes no POST."""
    app = _make_app()
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"output\n", None))
    conversation = MagicMock()

    with (
        patch.object(app, "query_one", return_value=conversation),
        patch(
            "archie_cli.tui.app.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("archie_cli.tui.app.httpx.AsyncClient") as http_client,
    ):
        await app._run_direct_shell("printf output")

    conversation.add_shell_output.assert_called_once_with("printf output", "output\n", exit_code=0)
    http_client.assert_not_called()


@pytest.mark.asyncio
async def test_direct_shell_cancel_renders_local_output():
    """Esc cancels local shell execution without creating a session event."""
    import asyncio

    app = _make_app()
    conversation = MagicMock()
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
    with (
        patch.object(app, "query_one", return_value=conversation),
        patch(
            "archie_cli.tui.app.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
    ):
        task = asyncio.create_task(app._run_direct_shell("long command"))
        while app._shell_proc is not process:
            await asyncio.sleep(0)
        app.action_cancel()
        await task

    conversation.add_shell_output.assert_called_once_with(
        "long command", "partial\n", exit_code=130
    )


@pytest.mark.asyncio
async def test_replay_events_reports_http_failure():
    """Replay failure is explicit instead of being treated as an empty history."""
    app = _make_app()
    response = MagicMock(status_code=503, text="unavailable")
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(return_value=response)

    with patch("archie_cli.tui.app.httpx.AsyncClient", return_value=mock_http):
        assert await app._replay_events() is False


@pytest.mark.asyncio
async def test_reconnect_retries_when_replay_fails():
    """Reconnect does not report success until canonical replay succeeds."""
    app = _make_app()
    app._ws.connect = AsyncMock()
    app._ws.disconnect = AsyncMock()
    app._receive_loop = AsyncMock()
    app._replay_events = AsyncMock(side_effect=[False, True])

    async def no_sleep(_delay):
        return None

    with (
        patch("archie_cli.tui.app.asyncio.sleep", side_effect=no_sleep),
        patch.object(app, "notify") as notify,
    ):
        await app._reconnect()

    assert app._ws.connect.await_count == 2
    assert app._replay_events.await_count == 2
    app._ws.disconnect.assert_awaited_once()
    assert any(call.args == ("Reconnected to agent",) for call in notify.call_args_list)


@pytest.mark.asyncio
async def test_replay_child_assistant_message_reconstructs_child_output():
    """Persisted child assistant messages rebuild child detail on replay."""
    app = _make_app()
    event = AssistantMessage(
        id="child-assistant",
        turn=1,
        scope="task-1",
        subagent_index=0,
        iteration=1,
        request_id="request-1",
        content="persisted child response",
        interrupted=False,
    )

    with patch.object(app, "_render_child"), patch.object(app, "_update_child_modal"):
        app._apply_event(event, historical=True)

    assert "persisted child response" in app._child_activity[("task-1", 0)].lines


@pytest.mark.asyncio
async def test_receive_disconnect_preserves_partial_stream_until_replay():
    """A transient socket close must not finalize partial assistant output."""
    app = _make_app()
    app._turn_active = True
    app._streaming = MagicMock()
    app._stream_text = "partial response"

    async def empty_receive():
        if False:
            yield None

    app._ws.receive = empty_receive
    with (
        patch.object(app, "_show_client_error"),
        patch.object(app, "_schedule_reconnect"),
        patch.object(app, "_end_turn") as end_turn,
    ):
        await app._receive_loop()

    end_turn.assert_not_called()
    assert app._stream_text == "partial response"
    assert app._turn_active is True


@pytest.mark.asyncio
async def test_child_replay_replaces_live_child_deltas():
    """Durable child content replaces partial live deltas after reconnect."""
    app = _make_app()
    live_delta = TextDelta(
        id="child-delta",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=0,
        request_id="request-1",
        text="partial ",
    )
    durable = AssistantMessage(
        id="child-assistant-reconnect",
        turn=1,
        scope="task-1",
        subagent_index=0,
        iteration=1,
        request_id="request-1",
        content="partial response",
        interrupted=False,
    )

    with patch.object(app, "_render_child"), patch.object(app, "_update_child_modal"):
        app._apply_event(live_delta)
        app._apply_event(durable, historical=True)

    assert app._child_activity[("task-1", 0)].lines == ["partial response"]


def test_child_replay_preserves_durable_lines_across_requests():
    """Reconnect reconciliation keeps each request's durable child answer."""
    app = _make_app()
    first_assistant = AssistantMessage(
        id="child-assistant-1",
        turn=1,
        scope="task-1",
        subagent_index=0,
        iteration=1,
        request_id="request-1",
        content="first answer",
        interrupted=False,
    )
    second_delta = TextDelta(
        id="child-delta-2",
        turn=1,
        iteration=2,
        scope="task-1",
        subagent_index=0,
        request_id="request-2",
        text="second partial",
    )
    second_assistant = AssistantMessage(
        id="child-assistant-2",
        turn=1,
        scope="task-1",
        subagent_index=0,
        iteration=2,
        request_id="request-2",
        content="second answer",
        interrupted=False,
    )

    with patch.object(app, "_render_child"), patch.object(app, "_update_child_modal"):
        app._apply_event(first_assistant, historical=True)
        app._apply_event(second_delta)
        app._apply_event(second_assistant, historical=True)

    assert app._child_activity[("task-1", 0)].lines == ["first answer", "second answer"]
    assert "second partial" not in app._child_activity[("task-1", 0)].lines


def test_sibling_child_streams_finalize_independently():
    """Finalizing one child request leaves a sibling transient request untouched."""
    app = _make_app()
    child_zero_delta = TextDelta(
        id="child-zero-delta",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=0,
        request_id="request-zero",
        text="zero partial",
    )
    child_one_delta = TextDelta(
        id="child-one-delta",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=1,
        request_id="request-one",
        text="one partial",
    )
    child_zero_assistant = AssistantMessage(
        id="child-zero-assistant",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=0,
        request_id="request-zero",
        content="zero answer",
        interrupted=False,
    )

    with patch.object(app, "_render_child"), patch.object(app, "_update_child_modal"):
        app._apply_event(child_zero_delta)
        app._apply_event(child_one_delta)
        app._apply_event(child_zero_assistant)

    zero_key = app._request_key(child_zero_assistant)
    one_key = app._request_key(child_one_delta)
    assert zero_key in app._finalized_assistant_requests
    assert zero_key not in app._transient_assistant_text
    assert app._transient_assistant_text[one_key] == "one partial"
    assert app._child_activity[("task-1", 0)].lines == ["zero answer"]
    assert app._child_activity[("task-1", 1)].lines == []
    assert app._child_activity[("task-1", 1)].activity == "Responding..."


@pytest.mark.parametrize("scope, subagent_index", [(None, None), ("task-1", 0)])
def test_replay_assistant_suppresses_matching_buffered_delta(scope, subagent_index):
    """Durable assistant content wins over a duplicate buffered live delta."""
    app = _make_app()
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
    app._event_buffer = [delta]
    with (
        patch.object(app, "query_one", return_value=MagicMock()),
        patch.object(app, "_render_child"),
        patch.object(app, "_update_child_modal"),
    ):
        app._apply_event(assistant, historical=True)
        app._dispatch_buffered_events()

    key = app._request_key(assistant)
    assert key in app._finalized_assistant_requests
    assert key not in app._transient_assistant_text


@pytest.mark.asyncio
async def test_direct_shell_timeout_renders_exit_124():
    app = _make_app()
    process = MagicMock(returncode=None)
    process.communicate = MagicMock()
    process.wait = AsyncMock()
    conversation = MagicMock()

    with (
        patch.object(app, "query_one", return_value=conversation),
        patch(
            "archie_cli.tui.app.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("archie_cli.tui.app.asyncio.wait_for", side_effect=TimeoutError),
    ):
        await app._run_direct_shell("sleep 60")

    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once()
    conversation.add_shell_output.assert_called_once_with("sleep 60", "(timed out)", exit_code=124)


@pytest.mark.asyncio
async def test_direct_shell_truncates_output_after_10000_lines():
    app = _make_app()
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(
        return_value=("\n".join(f"line-{index}" for index in range(10_001)).encode(), None)
    )
    conversation = MagicMock()

    with (
        patch.object(app, "query_one", return_value=conversation),
        patch(
            "archie_cli.tui.app.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
    ):
        await app._run_direct_shell("generate-lines")

    output = conversation.add_shell_output.call_args.args[1]
    assert output.splitlines()[-1] == "(truncated)"
    assert len(output.splitlines()) == 10_001
    assert output.splitlines()[0] == "line-0"
    assert output.splitlines()[-2] == "line-9999"


@pytest.mark.asyncio
async def test_direct_shell_failure_renders_local_error():
    app = _make_app()
    conversation = MagicMock()

    with (
        patch.object(app, "query_one", return_value=conversation),
        patch(
            "archie_cli.tui.app.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=OSError("docker unavailable"),
        ),
    ):
        await app._run_direct_shell("pwd")

    conversation.add_client_error.assert_called_once_with("Docker exec failed: docker unavailable")
    conversation.add_shell_output.assert_not_called()
