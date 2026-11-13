import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from archie_cli.tui.app import ArchieApp


async def _confirm_quit(app: ArchieApp, terminate: bool | None) -> None:
    """Open the public quit action and invoke the confirmation callback."""
    app.push_screen = MagicMock()
    await app.action_quit()
    callback = app.push_screen.call_args.args[1]
    await callback(terminate)


def _app() -> ArchieApp:
    return ArchieApp(
        ws_url="ws://localhost/stream",
        api_url="http://localhost/api/sessions/session-1",
        container_name="container",
    )


@pytest.mark.asyncio
async def test_quit_without_termination_disconnects_only():
    app = _app()
    app._ws.disconnect = AsyncMock()

    with patch.object(app, "exit") as exit_mock:
        await _confirm_quit(app, False)

    app._ws.disconnect.assert_awaited_once()
    exit_mock.assert_called_once_with()


@pytest.mark.asyncio
async def test_quit_with_termination_disconnects_before_delete():
    app = _app()
    events = []

    async def disconnect():
        events.append("disconnect")

    app._ws.disconnect = AsyncMock(side_effect=disconnect)
    response = MagicMock(status_code=200)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async def delete(*args, **kwargs):
        events.append("delete")
        return response

    client.delete = AsyncMock(side_effect=delete)

    with (
        patch("archie_cli.tui.client.httpx.AsyncClient", return_value=client),
        patch.object(app, "exit"),
    ):
        await _confirm_quit(app, True)

    assert events == ["disconnect", "delete"]
    client.delete.assert_awaited_once_with(app._api_url, timeout=15.0)
    app._ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_quit_cancels_reconnect_started_by_ensure_connected():
    """Quit cancels a reconnect started by a send path, not only the receive loop."""
    app = _app()
    app._editor_active = True
    app._ws.disconnect = AsyncMock()
    ensure_task = asyncio.create_task(app._ensure_connected())
    await asyncio.sleep(0)

    assert app._reconnect_task is not None
    with patch.object(app, "exit"):
        await _confirm_quit(app, False)

    with pytest.raises(asyncio.CancelledError):
        await ensure_task
    assert app._reconnect_task is None


@pytest.mark.asyncio
async def test_quit_cancels_receive_and_inflight_history_replay():
    """Quit cancels receive and history replay tasks before disconnecting."""
    app = _app()
    app._ws.disconnect = AsyncMock()
    history_requests = 0
    replay_cancelled = asyncio.Event()

    async def replay_events():
        nonlocal history_requests
        history_requests += 1
        try:
            await asyncio.Event().wait()
        finally:
            replay_cancelled.set()

    receive_task = asyncio.create_task(asyncio.Event().wait())
    reconnect_task = asyncio.create_task(replay_events())
    app._receive_task = receive_task
    app._reconnect_task = reconnect_task
    app._reconnecting = True
    await asyncio.sleep(0)

    with patch.object(app, "exit"):
        await _confirm_quit(app, False)

    assert receive_task.cancelled()
    assert reconnect_task.cancelled()
    assert history_requests == 1
    assert replay_cancelled.is_set()
    assert app._reconnect_task is None
    assert not app._reconnecting


@pytest.mark.asyncio
async def test_quit_cancels_reconnect_started_by_editor_recovery():
    """Quit cancels the reconnect that runs after the external editor closes."""
    app = _app()
    app._editor_active = True
    app._ws.disconnect = AsyncMock()
    recovery_task = asyncio.create_task(app._recover_after_editor())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert app._reconnect_task is not None
    reconnect_task = app._reconnect_task
    with patch.object(app, "exit"):
        await _confirm_quit(app, False)

    assert reconnect_task.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await recovery_task
    assert app._reconnect_task is None


def _delete_client(**delete_kwargs) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.delete = AsyncMock(**delete_kwargs)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 404, 409], ids=["stopped", "gone", "conflict"])
async def test_quit_with_termination_accepts_stopped_gone_or_conflict(status_code):
    """200 (stopped), 404 (already gone) and 409 (starting, stopping or ended) are all fine."""
    app = _app()
    app._ws.disconnect = AsyncMock()
    app._show_client_error = MagicMock()
    client = _delete_client(return_value=MagicMock(status_code=status_code))

    with (
        patch("archie_cli.tui.client.httpx.AsyncClient", return_value=client),
        patch.object(app, "exit") as exit_mock,
    ):
        await _confirm_quit(app, True)

    client.delete.assert_awaited_once()
    app._show_client_error.assert_not_called()
    exit_mock.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delete_kwargs", "message"),
    [
        ({"return_value": MagicMock(status_code=500)}, "Failed to terminate session: 500"),
        (
            {"side_effect": httpx.ConnectError("refused")},
            "Failed to terminate session: refused",
        ),
    ],
    ids=["error-status", "transport-error"],
)
async def test_quit_with_termination_reports_delete_failure(delete_kwargs, message):
    """A failed DELETE after client teardown is reported and quit still exits."""
    app = _app()
    events = []
    app._ws.disconnect = AsyncMock(side_effect=lambda: events.append("disconnect"))
    app._show_client_error = MagicMock(side_effect=lambda msg: events.append(msg))
    client = _delete_client(**delete_kwargs)

    with (
        patch("archie_cli.tui.client.httpx.AsyncClient", return_value=client),
        patch.object(app, "exit") as exit_mock,
    ):
        await _confirm_quit(app, True)

    assert events == ["disconnect", message]
    exit_mock.assert_called_once_with()
