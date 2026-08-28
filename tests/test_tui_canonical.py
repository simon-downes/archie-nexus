"""TUI-side canonical event handling: ledger accounting, reducer, replay.

These exercise the M4 client-side reconstruction path without a running
Textual app by stubbing `query_one` and the status-bar update.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_shared.canonical_events import (
    AssistantMessage,
    IterationStart,
    LLMRequest,
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
        turn_iteration="1.1",
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
        turn_iteration="1.0",
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


def test_render_canonical_deduplicates_by_id():
    app = _make_app()
    app._seen_event_ids = {"u1"}
    conv = MagicMock()
    with patch.object(app, "query_one", return_value=conv):
        app._render_canonical(UserMessage(id="u1", turn=1, scope=None, content="hi"))
    conv.add_user_message.assert_not_called()


def test_render_canonical_does_not_create_empty_iteration_block():
    """Text-only replay iterations do not leave an empty visual block."""
    app = _make_app()
    conv = MagicMock()
    with patch.object(app, "query_one", return_value=conv):
        app._render_canonical(IterationStart(id="i1", turn_iteration="1.1", scope=None, index=1))

    conv.begin_iteration.assert_not_called()


def test_render_canonical_reconstructs_tool_summary_client_side():
    """ToolCall input is cached so ToolResult renders a shared-formatter summary."""
    app = _make_app()
    block = MagicMock()
    conv = MagicMock()
    conv.begin_iteration.return_value = block
    with patch.object(app, "query_one", return_value=conv):
        app._render_canonical(IterationStart(id="i1", turn_iteration="1.1", scope=None, index=1))
        app._render_canonical(
            ToolCall(
                id="c1",
                turn_iteration="1.1",
                scope=None,
                request_id="r1",
                tool_use_id="t1",
                name="read",
                input={"path": "/etc/hosts"},
            )
        )
        app._render_canonical(
            ToolResult(
                id="r1e",
                turn_iteration="1.1",
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
async def test_replay_events_seeds_cost_from_ledger():
    """A full replay containing an llm_request folds its cost into accounting."""
    app = _make_app()
    events = [
        UserMessage(id="u1", turn=1, scope=None, content="hi"),
        _llm_request("l1", 0.25, input_tokens=200, output_tokens=20),
        AssistantMessage(
            id="a1", turn=1, turn_iteration="1.0", scope=None, request_ids=["l1"], content="ok", interrupted=False
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

    full_body = encode_event(UserMessage(id="u1", turn=1, scope=None, content="hi")) + "\n"
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
    assert app._last_event_id == "u1"
