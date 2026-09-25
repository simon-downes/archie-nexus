from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_cli.tui.app import ArchieApp


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
    app.action_quit = AsyncMock()

    with patch.object(app, "exit") as exit_mock:
        await app._finish_quit(False)

    app._ws.disconnect.assert_awaited_once()
    exit_mock.assert_called_once_with()


@pytest.mark.asyncio
async def test_quit_with_termination_deletes_session_before_disconnect():
    app = _app()
    app._ws.disconnect = AsyncMock()
    response = MagicMock(status_code=200)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.delete = AsyncMock(return_value=response)

    with patch("archie_cli.tui.app.httpx.AsyncClient", return_value=client):
        await app._finish_quit(True)

    client.delete.assert_awaited_once_with(app._api_url, timeout=15.0)
    app._ws.disconnect.assert_awaited_once()
