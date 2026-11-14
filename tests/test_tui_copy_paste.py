"""Copy and paste behavior in the Textual attach TUI."""

import pytest
from archie_cli.tui.app import ArchieApp
from archie_cli.tui.input import MessageInput
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static


class _SelectionParent(Widget):
    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static("selectable text", id="transcript")

    def get_copy_text(self) -> str:
        return "whole block"


class _CopyApp(App):
    """Runs ``ArchieApp.action_copy_block``; records its notices instead of showing them."""

    BINDINGS = [Binding("ctrl+c", "copy_block", show=False)]

    def __init__(self) -> None:
        super().__init__()
        self.notices: list[str] = []

    def notice(self, title: str, text: str = "", *args, **kwargs) -> None:
        self.notices.append(title)

    def action_copy_block(self) -> None:
        ArchieApp.action_copy_block(self)


class CopyTestApp(_CopyApp):
    """Textual app shell for clipboard tests without a WebSocket connection."""

    def compose(self) -> ComposeResult:
        yield _SelectionParent()


class _FocusableStatic(Static):
    can_focus = True


class _CopyParent(Widget):
    def compose(self) -> ComposeResult:
        yield _FocusableStatic("nested content")

    def get_copy_text(self) -> str:
        return "whole block"


class NestedCopyTestApp(_CopyApp):
    def compose(self) -> ComposeResult:
        yield _CopyParent(id="parent")


class PasteTestApp(App):
    """Textual app shell containing only the message input."""

    def compose(self) -> ComposeResult:
        yield MessageInput(id="input")

    def on_mount(self) -> None:
        self.query_one("#input", MessageInput).focus()


def _make_app(app_class: type[App]) -> App:
    return app_class()


@pytest.mark.asyncio
async def test_ctrl_c_copies_textual_selection_before_block_fallback():
    app = _make_app(CopyTestApp)

    async with app.run_test() as pilot:
        parent = app.query_one(_SelectionParent)
        app.set_focus(parent)
        transcript = app.query_one("#transcript", Static)

        await pilot.mouse_down(transcript, offset=(0, 0))
        await pilot.hover(transcript, offset=(5, 0))
        await pilot.mouse_up(transcript, offset=(5, 0))
        assert app.screen.get_selected_text() == "select"

        await pilot.press("ctrl+c")
        assert app.clipboard == "select"

        app.copy_to_clipboard("sentinel")
        app.action_copy_block()

        assert app.clipboard == "select"


@pytest.mark.asyncio
async def test_copy_falls_back_to_focused_parent_block():
    app = _make_app(NestedCopyTestApp)

    async with app.run_test() as pilot:
        nested = app.query_one(_FocusableStatic)
        app.set_focus(nested)

        await pilot.press("ctrl+c")

        assert app.clipboard == "whole block"
        assert app.notices == ["Copied to clipboard"]


@pytest.mark.asyncio
async def test_ctrl_v_pastes_textual_clipboard_into_message_input():
    app = _make_app(PasteTestApp)

    async with app.run_test() as pilot:
        app.copy_to_clipboard("pasted text")
        await pilot.press("ctrl+v")

        assert app.query_one("#input", MessageInput).text == "pasted text"


# --- In the multi-session app -----------------------------------------------------------


def _tool_events():
    from archie_shared.events import ToolCall, ToolResult, UserMessage

    common = {"turn": 1, "iteration": 1, "scope": None, "request_id": "r1", "tool_use_id": "t1"}
    return (
        UserMessage(id="u1", turn=1, scope=None, content="run it"),
        ToolCall(id="c-t1", name="exec", input={"source": "print(1)"}, **common),
        ToolResult(id="r-t1", content="1", is_error=False, duration_ms=3, **common),
    )


@pytest.mark.asyncio
async def test_ctrl_c_copies_the_focused_transcript_block_with_a_notice():
    from archie_cli.tui.conversation import UserMessage

    from tests.tui_fakes import apply_events, attached, make_app

    app = make_app()
    async with app.run_test(size=(170, 40)) as pilot:
        pane = await attached(app, pilot)
        await apply_events(app, pilot, *_tool_events())
        message = pane.conversation.query_one(UserMessage)
        message.focus()
        await pilot.press("ctrl+c")
        assert app.clipboard == "run it"
        assert [n.title for n in app.sidebar.notices.notices] == ["Copied to clipboard"]


@pytest.mark.asyncio
async def test_ctrl_c_in_a_tool_view_copies_the_whole_call_without_a_selection():
    from archie_cli.tui.detail import ToolDetailView

    from tests.tui_fakes import apply_events, attached, make_app

    app = make_app()
    async with app.run_test(size=(170, 40)) as pilot:
        pane = await attached(app, pilot)
        await apply_events(app, pilot, *_tool_events())
        pane.open_tool(app.controller.builder.tools["t1"])
        await pilot.pause()
        view = pane.top_view
        assert isinstance(view, ToolDetailView)
        view.body.focus()
        await pilot.pause()
        assert app.screen.get_selected_text() is None
        await pilot.press("ctrl+c")
        assert app.clipboard == "SOURCE\nprint(1)\n\nRESULT\n1"
        assert [n.title for n in app.sidebar.notices.notices] == ["Copied to clipboard"]
