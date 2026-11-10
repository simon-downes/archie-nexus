import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
        api_url="http://localhost/sessions/session-1",
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
        patch("archie_cli.tui.app.httpx.AsyncClient", return_value=client),
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
