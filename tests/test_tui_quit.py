"""Quitting the TUI closes the window after the quit dialog; the sessions keep running (084)."""

import asyncio

from archie_cli.tui.dialogs import QuitDialog

from tests.tui_fakes import FakeOrchestrator, attached, make_app, make_record, settle

SIZE = (150, 30)


def _client() -> FakeOrchestrator:
    return FakeOrchestrator(
        sessions=[
            make_record("ws-a", started_at="2026-11-12T10:01:00+00:00"),
            make_record("ws-b", started_at="2026-11-12T10:02:00+00:00"),
        ]
    )


async def _quit(app, pilot) -> None:
    """ctrl+q, then confirm the quit dialog (its Quit button has focus)."""
    await pilot.press("ctrl+q")
    await settle(pilot, lambda: isinstance(app.screen, QuitDialog))
    await pilot.press("enter")


async def test_ctrl_q_exits_after_the_dialog_without_stopping_sessions():
    client = _client()
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        await _quit(app, pilot)
        await settle(pilot, lambda: app.return_code is not None)
    assert client.deleted == []
    assert not app.controllers
    for session_id in ("ws-a", "ws-b"):
        stream = client.stream_for(session_id)
        stream.disconnect.assert_awaited()
        assert not stream.connected
    assert app.return_code == 0


async def test_quit_cancels_a_controller_still_starting():
    """A controller whose first history request hangs is cancelled, not awaited forever."""
    started = asyncio.Event()

    class HangingClient(FakeOrchestrator):
        async def events(self, session_id, after):
            started.set()
            await asyncio.Event().wait()

    client = HangingClient(sessions=[make_record("ws-a")])
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await asyncio.wait_for(started.wait(), 5)
        await _quit(app, pilot)
        await settle(pilot, lambda: app.return_code is not None)
    assert client.deleted == []
    client.stream_for("ws-a").disconnect.assert_awaited()


async def test_closing_the_app_another_way_also_closes_the_controllers():
    client = _client()
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
    client.stream_for("ws-a").disconnect.assert_awaited()
    assert client.deleted == []
