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


class CopyTestApp(App):
    """Textual app shell for clipboard tests without a WebSocket connection."""

    BINDINGS = [Binding("ctrl+c", "copy_block", show=False)]

    def compose(self) -> ComposeResult:
        yield _SelectionParent()

    def action_copy_block(self) -> None:
        ArchieApp.action_copy_block(self)


class _FocusableStatic(Static):
    can_focus = True


class _CopyParent(Widget):
    def compose(self) -> ComposeResult:
        yield _FocusableStatic("nested content")

    def get_copy_text(self) -> str:
        return "whole block"


class NestedCopyTestApp(App):
    BINDINGS = [Binding("ctrl+c", "copy_block", show=False)]

    def compose(self) -> ComposeResult:
        yield _CopyParent(id="parent")

    def action_copy_block(self) -> None:
        ArchieApp.action_copy_block(self)


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


@pytest.mark.asyncio
async def test_ctrl_v_pastes_textual_clipboard_into_message_input():
    app = _make_app(PasteTestApp)

    async with app.run_test() as pilot:
        app.copy_to_clipboard("pasted text")
        await pilot.press("ctrl+v")

        assert app.query_one("#input", MessageInput).text == "pasted text"
