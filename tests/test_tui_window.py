"""TUI transcript windowing: a bounded slice of rows is mounted, all history stays reachable."""

import pytest
from archie_cli.tui.transcript_view import TranscriptView, WindowBoundary
from archie_shared.events import AssistantMessage, UserMessage
from archie_shared.transcript import TranscriptBuilder
from textual.app import App
from textual.widgets import Static

WINDOW, PAGE, OVERSCAN = 20, 8, 4


class _Host(App):
    def compose(self):
        yield TranscriptView(id="view", window=WINDOW, page=PAGE, overscan=OVERSCAN)


def _mounted_keys(view: TranscriptView) -> list[str]:
    return [row.key for row in view.rows if row.widget is not None and row.widget.is_attached]


def _boundaries(view: TranscriptView) -> dict[str, int]:
    return {b.direction: b.hidden_count for b in view.query(WindowBoundary)}


def _add(view: TranscriptView, count: int) -> None:
    for i in range(count):
        view.add_local(lambda i=i: Static(f"line {i}\n" * 3))


async def _filled(pilot, view: TranscriptView, count: int) -> None:
    view.suspend()
    _add(view, count)
    view.resume()
    await pilot.pause()


@pytest.mark.asyncio
async def test_replay_mounts_only_the_latest_window():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        assert len(view.rows) == 100
        assert view.window == (80, 100)
        assert len(_mounted_keys(view)) == WINDOW
        assert _boundaries(view) == {"earlier": 80}
        assert view.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_scrolling_to_the_top_pages_earlier_and_keeps_the_anchor():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        anchor = view.rows[80].widget

        view.scroll_home(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert view.window == (72, 92)
        assert view.rows[80].widget is anchor and anchor.is_attached
        # The former first row stays at the top of the viewport.
        assert anchor.region.y - view.region.y == 0
        assert _boundaries(view) == {"earlier": 72, "later": 8}

        # Every earlier row is reachable.
        for _ in range(20):
            view.scroll_home(animate=False)
            await pilot.pause()
            await pilot.pause()
        assert view.window[0] == 0
        assert _boundaries(view) == {"later": 100 - view.window[1]}
        assert len(_mounted_keys(view)) <= WINDOW + OVERSCAN


@pytest.mark.asyncio
async def test_scrolling_to_the_bottom_of_a_shifted_window_pages_later():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        view.shift_window("earlier")
        await pilot.pause()
        view.shift_window("earlier")
        await pilot.pause()
        assert view.window == (64, 84)
        anchor = view.rows[83].widget

        view.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert view.window == (72, 92)
        assert view.rows[83].widget is anchor and anchor.is_attached


@pytest.mark.asyncio
async def test_live_rows_while_following_trim_the_top_beyond_the_overscan():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        _add(view, WINDOW + OVERSCAN)
        await pilot.pause()
        assert view.window == (0, WINDOW + OVERSCAN)
        _add(view, 1)
        await pilot.pause()
        assert view.window == (WINDOW + OVERSCAN + 1 - WINDOW, WINDOW + OVERSCAN + 1)
        assert len(_mounted_keys(view)) == WINDOW
        assert view.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_rows_added_while_reading_history_are_counted_not_mounted():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        view.shift_window("earlier")
        await pilot.pause()
        window = view.window
        _add(view, 3)
        await pilot.pause()
        assert view.window == window
        assert _boundaries(view)["later"] == 100 + 3 - window[1]


@pytest.mark.asyncio
async def test_a_new_user_message_returns_to_the_latest_window():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        builder = TranscriptBuilder()
        view.suspend()
        for turn in range(1, 31):
            for event in (
                UserMessage(id=f"u{turn}", turn=turn, scope=None, content=f"question {turn}"),
                AssistantMessage(
                    id=f"a{turn}",
                    turn=turn,
                    iteration=1,
                    scope=None,
                    request_id=f"r{turn}",
                    content=f"answer {turn}",
                    interrupted=False,
                ),
            ):
                view.apply_changes(builder, builder.apply(event))
        view.resume()
        await pilot.pause()
        assert len(view.rows) == 60

        view.shift_window("earlier")
        await pilot.pause()
        view.shift_window("earlier")
        await pilot.pause()
        assert not view.window_is_latest

        event = UserMessage(id="u31", turn=31, scope=None, content="question 31")
        view.apply_changes(builder, builder.apply(event))
        for _ in range(3):  # scroll_end lands after the mount refresh
            await pilot.pause()
        assert view.window_is_latest and view.window[1] == len(view.rows) == 61
        assert view.rows[-1].key == "msg-u31" and view.rows[-1].widget is not None
        assert view.is_vertical_scroll_end
        assert "later" not in _boundaries(view)


@pytest.mark.asyncio
async def test_replaying_1000_turns_through_the_app_mounts_a_bounded_window():
    from unittest.mock import MagicMock, patch

    from archie_cli.tui.transcript_view import WINDOW_OVERSCAN, WINDOW_ROWS, Conversation
    from archie_shared.events import encode_event

    from tests.test_tui_detail import _App

    events = []
    for turn in range(1, 1001):
        events.append(UserMessage(id=f"u{turn}", turn=turn, scope=None, content=f"q {turn}"))
        events.append(
            AssistantMessage(
                id=f"a{turn}",
                turn=turn,
                iteration=1,
                scope=None,
                request_id=f"r{turn}",
                content=f"a {turn}",
                interrupted=False,
            )
        )
    body = "".join(encode_event(e) + "\n" for e in events)
    http = MagicMock()
    http.__aenter__.return_value = http
    http.__aexit__.return_value = False

    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        with (
            patch("archie_cli.tui.app.httpx.AsyncClient", return_value=http),
            patch.object(app, "_update_accounting_status"),
        ):
            http.get = _async_return(MagicMock(status_code=200, text=body))
            assert await app._replay_events() is True
        await pilot.pause()
        conv = app.query_one(Conversation)
        assert len(app.transcript.turns) == 1000
        rows = [row for row in conv.rows if row.node is not None]  # minus local rows
        assert len(rows) == 2000
        mounted = [row for row in conv.rows if row.widget is not None]
        assert len(mounted) <= WINDOW_ROWS + WINDOW_OVERSCAN
        total = len(conv.rows)
        assert conv.window == (total - WINDOW_ROWS, total)
        assert _boundaries(conv) == {"earlier": total - WINDOW_ROWS}


def _async_return(value):
    async def get(*_args, **_kwargs):
        return value

    return get
