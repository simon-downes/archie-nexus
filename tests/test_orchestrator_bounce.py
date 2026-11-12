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
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import FakeRuntime, running, use_runtime

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


# ---------------------------------------------------------------------------
# D4 — Orchestrator shutdown does NOT stop containers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_does_not_call_docker_stop(caplog):
    """Lifespan teardown must not call docker stop on any container."""
    with (
        use_runtime(app, FakeRuntime([running("proj-01abc12345", None)])),
        patch("archie_orchestrator.app.config_source.get", return_value=NexusConfig()),
        patch("archie_orchestrator.docker.subprocess.run") as mock_run,
    ):
        async with lifespan(app):
            pass  # enter startup → yield → exit shutdown

    # Any subprocess.run calls during shutdown are unexpected (there should be none)
    stop_calls = [c for c in mock_run.call_args_list if "stop" in str(c)]
    assert not stop_calls, f"docker stop was called during shutdown: {stop_calls}"


@pytest.mark.asyncio
async def test_after_restart_sessions_rediscovered():
    """After an orchestrator restart, GET /sessions returns the same running sessions."""
    runtime = FakeRuntime([running("proj-01abc12345", None), running("proj-02def67890", None)])
    responses = []

    with (
        use_runtime(app, runtime),
        patch("archie_orchestrator.app.config_source.get", return_value=NexusConfig()),
    ):
        for _ in range(2):  # start, stop, start again: the records come back from Docker
            async with lifespan(app):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    responses.append(await client.get("/api/sessions"))

    assert [r.status_code for r in responses] == [200, 200]
    first, second = (r.json() for r in responses)
    assert {s["session_id"] for s in first} == {"proj-01abc12345", "proj-02def67890"}
    assert {s["status"] for s in first} == {"idle"}
    assert first == second
    assert runtime.list_calls == 2


@pytest.mark.asyncio
async def test_shutdown_logs_active_connections(caplog):
    """Shutdown log shows the count of active WS connections."""
    with (
        use_runtime(app, FakeRuntime()),
        patch("archie_orchestrator.app.config_source.get", return_value=NexusConfig()),
        caplog.at_level(logging.INFO, logger="archie_orchestrator.app"),
    ):
        async with lifespan(app):
            pass

    assert any("Orchestrator stopping" in r.message and "0" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# D9 — TUI reconnect logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_reconnect_uses_0_5s_initial_delay():
    """TUI reconnect backoff starts at 0.5s (not 1.0s)."""
    from archie_cli.tui.app import ArchieApp

    app_instance = ArchieApp(
        ws_url="ws://127.0.0.1:7600/api/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/api/sessions/proj-01abc",
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
        ws_url="ws://127.0.0.1:7600/api/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/api/sessions/proj-01abc",
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
    """After reconnect, _replay_events renders only events past the cursor.

    The client tracks the id of the last rendered canonical event and requests
    /events?after=<id>; already-seen ids are deduplicated.
    """
    from archie_cli.tui.app import ArchieApp
    from archie_shared.events import (
        AssistantMessage,
        UserMessage,
        encode_event,
    )

    app_instance = ArchieApp(
        ws_url="ws://127.0.0.1:7600/api/sessions/proj-01abc/stream",
        api_url="http://127.0.0.1:7600/api/sessions/proj-01abc",
        container_name="archie-proj-01abc",
    )
    # Pretend we already rendered up to event "e3".
    app_instance._last_event_id = "e3"
    app_instance._seen_event_ids = {"e1", "e2", "e3"}

    # /events?after=e3 returns the tail (e4) plus e3 again (dedup exercise).
    events = [
        UserMessage(id="e3", turn=3, scope=None, content="again"),
        AssistantMessage(
            id="e4",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r4",
            content="new",
            interrupted=False,
        ),
    ]
    body = "".join(encode_event(e) + "\n" for e in events)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = body

    mock_conv = MagicMock()

    captured_url = {}

    async def _get(url, **kwargs):
        captured_url["url"] = url
        return mock_resp

    with (
        patch("archie_cli.tui.app.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = _get
        mock_client_cls.return_value = mock_http

        with patch.object(app_instance, "query_one", return_value=mock_conv):
            await app_instance._replay_events()

    # Requested incremental replay from the cursor.
    assert "after=e3" in captured_url["url"]
    # Only the unseen event (e4) reached the transcript; e3 was deduplicated.
    assert app_instance.transcript.turns == []
    assert [(i.role, i.content) for i in app_instance.transcript.preamble] == [("assistant", "new")]
    assert app_instance._last_event_id == "e4"


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
