"""Error and compaction rows show arbitrary text literally and never parse it as markup.

Regression: these widgets used to build markup strings with ``textual.markup.escape``,
which doesn't escape every input. Text such as ``xx[/`` raised ``MarkupError`` at render
time and crashed the TUI, and every replay of that session crashed it again.
"""

import pytest
from archie_cli.tui import theme
from archie_cli.tui.conversation import CompactionEntry, ErrorMessage
from textual.app import App
from textual.widgets import Static

BS = chr(92)  # built with chr() so no literal backslash precedes a bracket in this file

HOSTILE = [
    "xxbb/1[/",
    "a1x" + BS + "/@1[/",
    "[/]",
    "[bold]unclosed",
    "list[str] [/x]",
    "tenants/" + BS + BS + "[slug" + BS + BS + "]/a.ts",
    "end" + BS,
    "[@click=app.quit]x[/]",
]


def _plain(static: Static) -> str:
    return str(static.render())


class _Host(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self):
        yield self._widget


@pytest.mark.parametrize("text", HOSTILE)
def test_error_message_content_is_literal_text(text):
    widget = ErrorMessage(text)
    assert widget.content.plain == f"✗ Error\n{text}"
    assert {str(span.style) for span in widget.content.spans} == {f"bold {theme.ERROR}"}


@pytest.mark.parametrize("text", HOSTILE)
def test_compaction_entry_shows_reason_and_error_literally(text):
    entry = CompactionEntry(text, 100)
    header = list(entry.compose())[0]
    assert header.content.plain == f"○ Compaction {text} (100 tokens)"
    entry.complete(None, 0.0, 5, error=text)
    assert entry._header.plain == f"● Compaction {text} (100 tokens, 5ms) Failed: {text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", HOSTILE)
async def test_hostile_error_text_renders_without_crashing(text):
    app = _Host(ErrorMessage(text))
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()
        assert text in _plain(app.query_one(ErrorMessage))
    assert app._exception is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", HOSTILE)
async def test_hostile_compaction_error_renders_without_crashing(text):
    entry = CompactionEntry("threshold", 100)
    app = _Host(entry)
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        entry.complete(None, 0.0, 5, error=text)
        await pilot.pause()
        assert _plain(entry.query_one(".tool-header", Static)).endswith(f"Failed: {text}")
    assert app._exception is None
