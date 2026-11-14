"""TUI transcript windowing: a bounded slice of rows is mounted, all history stays reachable."""

import pytest
from archie_cli.tui.transcript_view import TranscriptView, WindowBoundary
from archie_shared.events import AssistantMessage, UserMessage
from archie_shared.transcript import TranscriptBuilder
from textual.app import App
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widgets import Static

WINDOW, PAGE, OVERSCAN = 20, 8, 4
SCROLL_SPAN = 2 * WINDOW


class _Host(App):
    """A view whose pages drop rows at once (no growth), to test the window's moves."""

    def compose(self):
        yield TranscriptView(
            id="view", window=WINDOW, page=PAGE, overscan=OVERSCAN, scroll_span=WINDOW
        )


class _GrowHost(App):
    def compose(self):
        yield TranscriptView(
            id="view", window=WINDOW, page=PAGE, overscan=OVERSCAN, scroll_span=SCROLL_SPAN
        )


def _mounted_keys(view: TranscriptView) -> list[str]:
    return [row.key for row in view.rows if row.widget is not None and row.widget.is_attached]


def _boundaries(view: TranscriptView) -> dict[str, int]:
    return {b.direction: b.hidden_count for b in view.query(WindowBoundary)}


def _add(view: TranscriptView, count: int) -> None:
    for i in range(count):
        view.add_local(lambda i=i: Static(f"line {i}\n" * 3))


async def _page(pilot, view: TranscriptView, direction: str) -> None:
    """Page the window and let the anchor restore land."""
    view.shift_window(direction)
    for _ in range(3):
        await pilot.pause()


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
        # The former first row stays where it was, below the boundary row it showed:
        # the rows paged in are above the view.
        assert anchor.region.y - view.region.y == 1
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
        await _page(pilot, view, "earlier")
        await _page(pilot, view, "earlier")
        assert view.window == (64, 84)
        anchor = view.rows[83].widget
        # Paging keeps the rows in view in place, so the view is still at its bottom edge.
        assert view.is_vertical_scroll_end

        await _wheel(pilot, view, MouseScrollDown)
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
    from archie_cli.tui.transcript_view import (
        WINDOW_OVERSCAN,
        WINDOW_PAGE,
        WINDOW_ROWS,
        WINDOW_SCROLL_ROWS,
    )

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

        # The default window fills the viewport, so scrolling pages it; the rows paged
        # past stay mounted (up to the scroll span) while reading, and back at the end
        # the window trims to its base size (beyond the overscan).
        assert conv.max_scroll_y > 0
        assert WINDOW_SCROLL_ROWS >= WINDOW_ROWS + 2 * WINDOW_PAGE
        assert 2 * WINDOW_PAGE > WINDOW_OVERSCAN
        for pages in (1, 2):
            conv.scroll_home(animate=False)
            for _ in range(4):
                await pilot.pause()
            assert conv.window == (total - WINDOW_ROWS - pages * WINDOW_PAGE, total)
        conv.scroll_end(animate=False)
        for _ in range(4):
            await pilot.pause()
        assert conv.window == (total - WINDOW_ROWS, total)
        assert conv.is_vertical_scroll_end

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


# -- scrolling at the window's edges ---------------------------------------------------


async def _wheel(pilot, view: TranscriptView, event: type, times: int = 1) -> None:
    """Send mouse-wheel events to the view, then let them land.

    The pilot has no wheel helper. Posted to the view, the events reach its handlers as
    the screen's forwarding would after bubbling up from the row under the pointer (the
    rows don't scroll), all before one pause.
    """
    region = view.region
    for _ in range(times):
        view.post_message(
            event(
                None,
                x=2,
                y=2,
                delta_x=0,
                delta_y=0,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=region.x + 2,
                screen_y=region.y + 2,
            )
        )
    for _ in range(4):
        await pilot.pause()


async def _scroll(pilot, view: TranscriptView, how: str, direction: str) -> None:
    """One scroll step toward ``direction`` (``"up"``/``"down"``) by wheel or key."""
    if how == "wheel":
        await _wheel(pilot, view, MouseScrollUp if direction == "up" else MouseScrollDown)
        return
    view.focus()
    await pilot.press(how)
    for _ in range(4):
        await pilot.pause()


def _top_row(view: TranscriptView) -> tuple[int, int]:
    """The first row at least partly in view, and its top's offset from the view's top."""
    top = view.scroll_y
    for i in range(*view.window):
        widget = view.rows[i].widget
        region = widget.virtual_region if widget is not None else None
        if region and region.bottom > top:
            return i, region.y - round(top)
    raise AssertionError("no row in view")


def _gap_rows(view: TranscriptView, visible: int, hidden: int, after: int) -> None:
    """``visible`` three-line rows, ``hidden`` rows with no height, ``after`` more rows.

    A hidden row is what an interrupted empty reply mounts (``widget_for``); a page of
    them adds no lines, so paging past them leaves the view where it was.
    """

    def hidden_row() -> Static:
        widget = Static("hidden")
        widget.display = False
        return widget

    _add(view, visible)
    for _ in range(hidden):
        view.add_local(hidden_row)
    _add(view, after)


@pytest.mark.asyncio
async def test_wheeling_down_from_the_top_reaches_the_latest_rows():
    """Regression: wheeling down got stuck at the bottom of a paged window (rows below)."""
    app = _Host()
    async with app.run_test(size=(80, 40)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        for i in range(100):
            view.add_local(lambda i=i: Static(f"line {i}\n" * (1 + i % 4)))
        view.resume()
        await pilot.pause()
        while view.window[0] > 0:
            await _page(pilot, view, "earlier")
        view.scroll_home(animate=False)
        await pilot.pause()

        for _ in range(200):
            if view.window_is_latest and view.is_vertical_scroll_end:
                break
            before = view.window, view.scroll_y
            await _wheel(pilot, view, MouseScrollDown)
            assert (view.window, view.scroll_y) != before, f"stuck at {before}"
        assert view.window_is_latest and view.is_vertical_scroll_end


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["wheel", "down", "pagedown"])
async def test_scrolling_down_at_the_bottom_edge_pages_later_rows_in(how):
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _gap_rows(view, 60, 3 * PAGE, 100 - 60 - 3 * PAGE)
        view.resume()
        await pilot.pause()
        while view.window[1] > 60:
            await _page(pilot, view, "earlier")
        # Paging earlier from the bottom keeps the bottom rows in view: the view is at
        # its bottom edge with rows below, where a scroll down doesn't move it.
        assert view.window[1] == 60 and view.is_vertical_scroll_end

        end = view.window[1]
        await _scroll(pilot, view, how, "down")
        assert view.window[1] > end, f"{how} at the bottom edge didn't page"
        for _ in range(100):
            if view.window_is_latest and view.is_vertical_scroll_end:
                break
            before = view.window, view.scroll_y
            await _scroll(pilot, view, how, "down")
            assert (view.window, view.scroll_y) != before, f"{how} stuck at {before}"
        assert view.window_is_latest and view.is_vertical_scroll_end


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["wheel", "up", "pageup"])
async def test_scrolling_up_at_the_top_edge_pages_earlier_rows_in(how):
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _gap_rows(view, 10, 3 * PAGE, 100 - 10 - 3 * PAGE)
        view.resume()
        await pilot.pause()
        while view.window[0] > 10 + 3 * PAGE:
            await _page(pilot, view, "earlier")
        # Scrolling to the top pages hidden rows in: the view stays at its top edge.
        view.scroll_home(animate=False)
        for _ in range(4):
            await pilot.pause()
        assert view.window[0] < 10 + 3 * PAGE and view.scroll_y == 0

        start = view.window[0]
        await _scroll(pilot, view, how, "up")
        assert view.window[0] < start, f"{how} at the top edge didn't page"
        for _ in range(100):
            if view.window[0] == 0 and view.scroll_y == 0:
                break
            before = view.window, view.scroll_y
            await _scroll(pilot, view, how, "up")
            assert (view.window, view.scroll_y) != before, f"{how} stuck at {before}"
        assert view.window[0] == 0 and view.scroll_y == 0


@pytest.mark.asyncio
async def test_a_scroll_at_the_edge_while_a_page_is_pending_is_not_lost():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _gap_rows(view, 60, 3 * PAGE, 100 - 60 - 3 * PAGE)
        view.resume()
        await pilot.pause()
        while view.window[1] > 60:
            await _page(pilot, view, "earlier")
        end = view.window[1]
        assert end == 60 and view.is_vertical_scroll_end

        # Two wheel steps in one frame: the first pages, the second arrives while that
        # page's restore is pending. The page adds no lines, so the second pages again.
        await _wheel(pilot, view, MouseScrollDown, times=2)
        assert view.window[1] == end + 2 * PAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "host"),
    # (Not earlier on ``_Host``: it drops the rows below, which near the window's end are
    # in view, so the view clamps; the default scroll span keeps them.)
    [("earlier", _GrowHost), ("later", _GrowHost), ("later", _Host)],
)
async def test_paging_keeps_the_rows_in_view_where_they_were(direction, host):
    app = host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        for i in range(100):
            view.add_local(lambda i=i: Static(f"line {i}\n" * (1 + i % 4)))
        view.resume()
        await pilot.pause()
        for _ in range(4):
            await _page(pilot, view, "earlier")
        if direction == "later":
            view.scroll_end(animate=False)
        else:
            view.scroll_home(animate=False)
        for _ in range(4):
            await pilot.pause()
        window = view.window
        row, offset = _top_row(view)
        await _page(pilot, view, direction)
        assert view.window != window
        # The row that was at the top of the view is still there: content doesn't jump.
        widget = view.rows[row].widget
        assert widget is not None and widget.is_attached
        moved = widget.virtual_region.y - round(view.scroll_y) - offset
        assert moved == 0


@pytest.mark.asyncio
async def test_paging_keeps_rows_mounted_up_to_the_scroll_span():
    app = _GrowHost()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        assert view.window == (100 - WINDOW, 100)

        # Earlier pages keep the rows below until the window holds SCROLL_SPAN rows.
        for pages in range(1, 6):
            await _page(pilot, view, "earlier")
            start = 100 - WINDOW - pages * PAGE
            assert view.window == (start, min(100, start + SCROLL_SPAN))
            assert len(_mounted_keys(view)) == view.window[1] - view.window[0]
        assert view.window[1] - view.window[0] == SCROLL_SPAN

        # Later pages keep the rows above, also up to SCROLL_SPAN.
        start, end = view.window
        await _page(pilot, view, "later")
        assert view.window == (start + PAGE, end + PAGE)

        # Jumping to the latest rows mounts the base window again.
        view.jump_to_latest()
        for _ in range(3):
            await pilot.pause()
        assert view.window == (100 - WINDOW, 100)
        assert len(_mounted_keys(view)) == WINDOW


@pytest.mark.asyncio
async def test_reaching_the_edge_while_a_page_is_pending_pages_again():
    """A scrollbar drag held at the bottom: its scroll reached the edge mid-page."""
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        view.suspend()
        _gap_rows(view, 60, 3 * PAGE, 100 - 60 - 3 * PAGE)
        view.resume()
        await pilot.pause()
        while view.window[1] > 60:
            await _page(pilot, view, "earlier")
        end, y = view.window[1], view.scroll_y
        assert end == 60 and view.is_vertical_scroll_end

        view.shift_window("later")  # pending until its restore; the page adds no lines
        view.scroll_to(y=y - 2, animate=False, immediate=True)  # the drag leaves the edge
        view.scroll_to(y=y, animate=False, immediate=True)  # and returns, mid-page
        for _ in range(4):
            await pilot.pause()
        assert view.window[1] == end + 2 * PAGE


@pytest.mark.asyncio
async def test_scrolling_back_to_the_end_trims_a_grown_window():
    app = _GrowHost()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        for _ in range(2):
            view.scroll_home(animate=False)  # pages earlier, keeping the rows below
            for _ in range(4):
                await pilot.pause()
        assert view.window == (100 - WINDOW - 2 * PAGE, 100)

        view.scroll_end(animate=False)
        for _ in range(4):
            await pilot.pause()
        assert view.window == (100 - WINDOW, 100)
        assert len(_mounted_keys(view)) == WINDOW
        assert view.is_vertical_scroll_end and view.following


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["append", "resize"])
async def test_a_grown_window_following_the_end_trims_on_append_or_resize(how):
    app = _GrowHost()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        # Paging earlier from the end keeps the bottom rows in view: still following.
        await _page(pilot, view, "earlier")
        await _page(pilot, view, "earlier")
        assert view.window == (100 - WINDOW - 2 * PAGE, 100) and view.following

        if how == "append":
            _add(view, 1)
            total = 101
        else:
            await pilot.resize_terminal(80, 15)  # the input grew
            total = 100
        for _ in range(4):
            await pilot.pause()
        assert view.window == (total - WINDOW, total)
        assert len(_mounted_keys(view)) == WINDOW
        assert view.is_vertical_scroll_end and view.following


@pytest.mark.asyncio
async def test_a_page_that_fails_doesnt_block_later_pages(monkeypatch):
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)

        def fail(self, *args, **kwargs):
            raise RuntimeError("mount failed")

        monkeypatch.setattr(TranscriptView, "_set_window", fail)
        with pytest.raises(RuntimeError):
            view.shift_window("earlier")
        monkeypatch.undo()
        assert view.window == (80, 100)

        view.scroll_home(animate=False)
        for _ in range(3):
            await pilot.pause()
        assert view.window == (72, 92)


@pytest.mark.asyncio
async def test_home_and_end_jump_to_the_first_and_latest_rows():
    app = _Host()
    async with app.run_test(size=(80, 20)) as pilot:
        view = app.query_one(TranscriptView)
        await _filled(pilot, view, 100)
        view.focus()

        await pilot.press("home")
        for _ in range(3):
            await pilot.pause()
        assert view.window == (0, WINDOW) and view.scroll_y == 0
        assert _boundaries(view) == {"later": 100 - WINDOW}

        await pilot.press("end")
        for _ in range(3):
            await pilot.pause()
        assert view.window == (100 - WINDOW, 100)
        assert view.is_vertical_scroll_end and view.following
