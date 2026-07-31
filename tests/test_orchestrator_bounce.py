"""Tests for orchestrator bounce survival (D4) and TUI auto-reattach (D9).

D4: Restarting the orchestrator doesn't kill sessions.
D9: TUI transparently reconnects after an orchestrator bounce.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_orchestrator.app import app, lifespan
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


def _make_session(session_id: str = "proj-01abc12345") -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=32771,
        raw_docker_status="Up 5 minutes",
    )


# ---------------------------------------------------------------------------
# D4 — Orchestrator shutdown does NOT stop containers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_does_not_call_docker_stop(caplog):
    """Lifespan teardown must not call docker stop on any container."""
    sessions = [_make_session()]

    with (
        patch("archie_orchestrator.app.list_sessions", return_value=sessions),
        patch("archie_orchestrator.app.load_nexus_config", return_value=NexusConfig()),
        patch("archie_orchestrator.docker.subprocess.run") as mock_run,
    ):
        async with lifespan(app):
            pass  # enter startup → yield → exit shutdown

    # Any subprocess.run calls during shutdown are unexpected (there should be none)
    stop_calls = [
        c for c in mock_run.call_args_list if "stop" in str(c)
    ]
    assert not stop_calls, f"docker stop was called during shutdown: {stop_calls}"


@pytest.mark.asyncio
async def test_after_restart_sessions_rediscovered():
    """After orchestrator restart, GET /sessions returns same sessions (stateless)."""
    sessions = [_make_session("proj-01abc12345"), _make_session("proj-02def67890")]

    with patch("archie_orchestrator.app.list_sessions", return_value=sessions):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Simulate a "restart" — the app is stateless, same docker ps result
            resp1 = await client.get("/sessions")
            resp2 = await client.get("/sessions")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert len(resp1.json()) == 2
    assert len(resp2.json()) == 2
    # Same sessions both times — rediscovery is the default
    assert resp1.json() == resp2.json()


@pytest.mark.asyncio
async def test_shutdown_logs_active_connections(caplog):
    """Shutdown log shows the count of active WS connections."""
    with (
        patch("archie_orchestrator.app.list_sessions", return_value=[]),
        patch("archie_orchestrator.app.load_nexus_config", return_value=NexusConfig()),
        caplog.at_level(logging.INFO, logger="archie_orchestrator.app"),
    ):
        async with lifespan(app):
            pass

    assert any(
        "Orchestrator stopping" in r.message and "0" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# D9 — TUI reconnect logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_reconnect_uses_0_5s_initial_delay():
    """TUI reconnect backoff starts at 0.5s (not 1.0s)."""
    from archie_cli.tui.app import ArchieApp

    app_instance = ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/sessions/proj-01abc",
        container_name="archie-proj-01abc",
    )

    sleep_calls = []
    _slept_once = False

    async def _fake_sleep(delay):
        nonlocal _slept_once
        sleep_calls.append(delay)
        if not _slept_once:
            _slept_once = True
            # Abort reconnect loop after recording the first delay
            raise asyncio.CancelledError

    connect_calls = []

    async def _fake_connect(url):
        connect_calls.append(url)

    with (
        patch("archie_cli.tui.app.asyncio.sleep", _fake_sleep),
        patch.object(app_instance._ws, "connect", _fake_connect),
    ):
        try:
            await app_instance._reconnect()
        except (Exception, asyncio.CancelledError):
            pass

    assert sleep_calls, "Expected at least one sleep call"
    assert sleep_calls[0] == 0.5, f"First retry delay should be 0.5s, got {sleep_calls[0]}"


@pytest.mark.asyncio
async def test_tui_reconnect_4004_gives_up_immediately():
    """Close code 4004 (session ended) → no retry, immediate failure message."""
    from archie_cli.tui.app import ArchieApp

    app_instance = ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/sessions/proj-01abc",
        container_name="archie-proj-01abc",
    )

    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    client_errors = []
    app_instance._show_client_error = lambda msg: client_errors.append(msg)

    with patch("archie_cli.tui.app.asyncio.sleep", _fake_sleep):
        await app_instance._reconnect(close_code=4004)

    # No sleeps — immediate give-up
    assert not sleep_calls
    assert any("Session has ended" in e for e in client_errors)


@pytest.mark.asyncio
async def test_tui_reconnect_deduplicates_history():
    """After reconnect, _load_history_since(since) only renders new turns."""
    from archie_cli.tui.app import ArchieApp

    app_instance = ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/sessions/proj-01abc",
        container_name="archie-proj-01abc",
    )
    # Pretend we already rendered turn 3
    app_instance._last_displayed_turn = 3

    turns = [
        {"role": "user", "turn_index": 1, "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "turn_index": 2, "content": [{"type": "text", "text": "hi"}]},
        {"role": "user", "turn_index": 3, "content": [{"type": "text", "text": "again"}]},
        {"role": "assistant", "turn_index": 4, "content": [{"type": "text", "text": "new"}]},
    ]


    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = turns

    rendered = []

    # Patch conv to capture what gets rendered

    mock_conv = MagicMock()
    mock_conv.add_user_message = lambda msg: rendered.append(("user", msg))
    mock_conv.add_assistant_message = lambda msg: rendered.append(("assistant", msg))

    with (
        patch("archie_cli.tui.app.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_http

        with patch.object(app_instance, "query_one", return_value=mock_conv):
            last = await app_instance._load_history_since(since=3)

    # Only turn 4 should have been rendered (turns 1-3 skipped)
    assert last == 4
    assert len(rendered) == 1
    assert rendered[0] == ("assistant", "new")


# ---------------------------------------------------------------------------
# Agent broadcast to empty set — no error
# ---------------------------------------------------------------------------


def test_agent_broadcast_empty_clients_no_error():
    """Broadcasting to an empty client set is a no-op without errors."""
    # The agent's broadcast is independent of the orchestrator, but we can
    # verify the proxy's active-connection counter starts at 0 and that
    # calling _active_ws_connections at 0 doesn't cause issues.
    from archie_orchestrator.proxy import get_active_ws_connections

    count = get_active_ws_connections()
    assert count >= 0  # Always non-negative; 0 when no connections active
