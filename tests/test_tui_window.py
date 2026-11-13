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
async def test_jumping_to_latest_cancels_a_pending_anchor_restore():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        view.shift_window("later")  # no-op at the latest window
        view.shift_window("earlier")  # schedules the anchor restore
        view.jump_to_latest()  # before the restore runs
        for _ in range(3):
            await pilot.pause()
        assert view.window == (80, 100)
        assert view.is_vertical_scroll_end


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
    from archie_cli.tui.transcript_view import WINDOW_OVERSCAN, WINDOW_PAGE, WINDOW_ROWS

    from tests.tui_fakes import FakeOrchestrator, attached, make_app, make_record

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
    client = FakeOrchestrator({"s1": events}, sessions=[make_record("s1")])

    app = make_app(client)
    async with app.run_test(size=(170, 40)) as pilot:
        pane = await attached(app, pilot)
        await pilot.pause()
        conv = pane.conversation
        assert len(app.controller.builder.turns) == 1000
        rows = [row for row in conv.rows if row.node is not None]  # minus local rows
        assert len(rows) == 2000
        mounted = [row for row in conv.rows if row.widget is not None]
        assert len(mounted) <= WINDOW_ROWS + WINDOW_OVERSCAN
        total = len(conv.rows)
        assert conv.window == (total - WINDOW_ROWS, total)
        assert _boundaries(conv) == {"earlier": total - WINDOW_ROWS}

        # The default window fills the viewport, so scrolling pages it both ways.
        assert conv.max_scroll_y > 0
        conv.scroll_home(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.window == (total - WINDOW_ROWS - WINDOW_PAGE, total - WINDOW_PAGE)
        conv.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert conv.window == (total - WINDOW_ROWS, total)

        # Shown again after a switch, the pane is rebuilt from the controller with the
        # same bounded window.
        client.records["s2"] = make_record("s2", started_at="2026-11-12T11:00:00+00:00")
        app.records["s2"] = client.records["s2"]
        await app.switch_to("s2")
        await attached(app, pilot, "s2")
        await app.switch_to("s1")
        pane = await attached(app, pilot, "s1")
        await pilot.pause()
        conv = pane.conversation
        assert len(conv.rows) == total
        assert conv.window == (total - WINDOW_ROWS, total)
        assert client.full_loads("s1") == 1


class _TallHost(App):
    def compose(self):
        yield TranscriptView(id="view", window=6, page=4, overscan=2)


async def _settled(pilot, view: TranscriptView, condition) -> None:
    for _ in range(40):
        if condition():
            return
        await pilot.pause()
    raise AssertionError(f"not settled: window {view.window}, max_scroll_y {view.max_scroll_y}")


def _one_line_rows(view: TranscriptView, count: int, label: str = "line") -> None:
    for i in range(count):
        view.add_local(lambda i=i: Static(f"{label} {i}"))


@pytest.mark.asyncio
async def test_a_window_shorter_than_the_viewport_pages_earlier_rows_in():
    app = _TallHost()
    async with app.run_test(size=(80, 30)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _one_line_rows(view, 100)
        view.resume()
        # Six one-line rows can't scroll in 30 lines; earlier rows page in until they can.
        await _settled(pilot, view, lambda: view.max_scroll_y > 0)
        start, end = view.window
        assert end == 100 and start < 100 - 6 and (100 - start) % 4 == 2
        assert view.is_vertical_scroll_end  # still following the end
        assert _boundaries(view) == {"earlier": start}
        # Scrolling to the top now pages further back, as for a full window.
        view.scroll_home(animate=False)
        await _settled(pilot, view, lambda: view.window[0] < start)
        for _ in range(5):
            await pilot.pause()  # let the page's anchor restore land
        # The window keeps its larger size: appends at the end don't shrink it back.
        view.jump_to_latest()
        await pilot.pause()
        assert view.window == (start, 100)
        _one_line_rows(view, 10, "more")
        await pilot.pause()
        assert view.window[1] == 110 and view.window[1] - view.window[0] >= 100 - start
        assert view.max_scroll_y > 0


@pytest.mark.asyncio
async def test_growing_the_viewport_pages_earlier_rows_in():
    app = _TallHost()
    async with app.run_test(size=(80, 5)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _one_line_rows(view, 100)
        view.resume()
        await pilot.pause()
        await pilot.pause()
        assert view.window == (94, 100)  # six rows and a boundary fill 5 lines
        await pilot.resize_terminal(80, 40)
        await _settled(pilot, view, lambda: view.window[0] < 94 and view.max_scroll_y > 0)
        assert view.window[1] == 100


@pytest.mark.asyncio
async def test_filling_stops_at_the_first_row_and_after_its_page_limit(monkeypatch):
    from archie_cli.tui import transcript_view

    app = _TallHost()
    async with app.run_test(size=(80, 60)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _one_line_rows(view, 20)
        view.resume()
        await _settled(pilot, view, lambda: view.window == (0, 20))
        assert view.max_scroll_y == 0 and _boundaries(view) == {}

    monkeypatch.setattr(transcript_view, "FILL_PAGES", 2)
    app = _TallHost()
    async with app.run_test(size=(80, 60)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _one_line_rows(view, 100)
        view.resume()
        for _ in range(20):
            await pilot.pause()
        assert view.window == (100 - 6 - 2 * 4, 100)
