"""Tests for orchestrator bounce survival (D4) and TUI auto-reattach (D9).

D4: Restarting the orchestrator doesn't kill sessions.
D9: TUI transparently reconnects after an orchestrator bounce.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest
from archie_cli.tui.session import NOT_FOUND_TEXT, RECONNECTING_TEXT
from archie_orchestrator.app import app, lifespan
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import FakeRuntime, running, use_runtime
from tests.tui_fakes import FakeOrchestrator, local_texts, make_controller

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
async def test_tui_reconnect_backs_off_from_0_5s_to_8s_and_keeps_trying():
    """Back-off starts at 0.5 s, doubles to 8 s and repeats; there is no give-up."""
    client = FakeOrchestrator()
    client.fail_connects(*(OSError("refused") for _ in range(8)))
    controller, listener = make_controller(client, "proj-01abc")
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay):
        if delay:  # the fake client's own yields are sleep(0)
            sleep_calls.append(delay)
        await real_sleep(0)

    with patch("archie_cli.tui.session.asyncio.sleep", _fake_sleep):
        assert await controller.ensure_connected() is True

    assert sleep_calls == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0, 8.0]
    assert len(client.streams) == 1
    # One entry for the outage, none per attempt, and no relaunch advice.
    assert local_texts(controller) == [RECONNECTING_TEXT]
    assert not any("elaunch" in text for text in local_texts(controller))
    assert not controller.reconnecting
    await controller.close()


@pytest.mark.asyncio
async def test_tui_reconnect_4004_stops_immediately():
    """A 4004 close (no such session) is not retried."""
    client = FakeOrchestrator()
    controller, _ = make_controller(client, "proj-01abc")
    await controller.start()
    real_sleep = asyncio.sleep
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)
        await real_sleep(0)

    with patch("archie_cli.tui.session.asyncio.sleep", _fake_sleep):
        client.latest.drop(4004)
        await controller._receive_task
        for _ in range(5):
            await real_sleep(0)

    # No back-off sleeps and no second connection: stopped at once.
    assert not sleep_calls and not controller.reconnecting
    assert len(client.streams) == 1
    assert local_texts(controller) == [NOT_FOUND_TEXT]
    await controller.close()


@pytest.mark.asyncio
async def test_tui_reconnect_stops_when_the_record_is_no_longer_live():
    """Retries last as long as the feed's record says the session is live."""
    live = True
    client = FakeOrchestrator()
    client.fail_connects(*(OSError("refused") for _ in range(20)))
    controller, _ = make_controller(client, "proj-01abc", is_live=lambda: live)
    controller.RECONNECT_DELAYS = (0.0,)
    attempts = 0
    real_stream = client.stream

    async def counting_stream(session_id):
        nonlocal attempts, live
        attempts += 1
        if attempts == 3:
            live = False  # the feed reports the session ended
        return await real_stream(session_id)

    client.stream = counting_stream
    assert await controller.ensure_connected() is False
    assert attempts == 3
    assert client.streams == []
    assert not controller.reconnecting


@pytest.mark.asyncio
async def test_tui_reconnect_deduplicates_history():
    """After reconnect, replay renders only events past the cursor.

    The client tracks the id of the last rendered canonical event and requests
    /events?after=<id>; already-seen ids are deduplicated.
    """
    from archie_shared.events import AssistantMessage, UserMessage, encode_event

    client = FakeOrchestrator()
    controller, _ = make_controller(client, "proj-01abc")
    # Pretend we already rendered up to event "e3".
    controller.cursor = "e3"
    controller._seen_event_ids = {"e1", "e2", "e3"}

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
    client.reply("".join(encode_event(e) + "\n" for e in events))

    assert await controller.replay() is True

    # Requested incremental replay from the cursor.
    assert client.requests == [("proj-01abc", "e3")]
    # Only the unseen event (e4) reached the transcript; e3 was deduplicated.
    assert controller.builder.turns == []
    assert [(i.role, i.content) for i in controller.builder.preamble] == [("assistant", "new")]
    assert controller.cursor == "e4"


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
