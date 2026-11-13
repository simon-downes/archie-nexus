"""TUI streaming: assistant text is written to a MarkdownStream fragment by fragment."""

from unittest.mock import patch

import pytest
from archie_cli.tui.conversation import AssistantMessage as AssistantRow
from archie_cli.tui.conversation import StreamingMessage
from archie_cli.tui.transcript_view import Conversation
from archie_shared.events import AssistantMessage, TextDelta, UserMessage
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownStream

from tests.test_tui_detail import _app, _feed

USER = UserMessage(id="u1", turn=1, scope=None, content="go")


def _delta(n: int, text: str) -> TextDelta:
    return TextDelta(id=f"d{n}", turn=1, iteration=1, scope=None, request_id="r1", text=text)


FRAGMENTS = ["# Title\n\n", "First para", "graph text.\n\n", "- one\n", "- two\n"]


async def _settle(pilot) -> None:
    for _ in range(5):
        await pilot.pause()


@pytest.mark.asyncio
async def test_deltas_are_written_as_fragments_without_full_text_updates():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _feed(app, pilot, USER, _delta(0, FRAGMENTS[0]))
        await _settle(pilot)
        stream = app.query_one(Conversation).query_one(StreamingMessage)
        markdown = stream.query_one(Markdown)

        writes: list[str] = []
        real_write = MarkdownStream.write

        async def spy(self, fragment):
            writes.append(fragment)
            await real_write(self, fragment)

        with (
            patch.object(MarkdownStream, "write", spy),
            patch.object(Markdown, "update", side_effect=AssertionError("full update")),
        ):
            for n, fragment in enumerate(FRAGMENTS[1:], start=1):
                await _feed(app, pilot, _delta(n, fragment))
                await _settle(pilot)

        assert "".join(writes) == "".join(FRAGMENTS[1:])
        assert all(len(w) < len("".join(FRAGMENTS)) for w in writes)
        assert markdown.source == "".join(FRAGMENTS)
        assert [type(b).__name__ for b in markdown.children] == [
            "MarkdownH1",
            "MarkdownParagraph",
            "MarkdownBulletList",
        ]


@pytest.mark.asyncio
async def test_final_message_with_same_text_keeps_the_streamed_markdown():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _feed(app, pilot, USER, *[_delta(n, f) for n, f in enumerate(FRAGMENTS)])
        await _settle(pilot)
        stream = app.query_one(Conversation).query_one(StreamingMessage)

        final = AssistantMessage(
            id="a1",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r1",
            content="".join(FRAGMENTS),
            interrupted=False,
        )
        await _feed(app, pilot, final)
        await _settle(pilot)
        conv = app.query_one(Conversation)
        assert conv.query_one(StreamingMessage) is stream  # kept, not rebuilt
        assert not conv.query(AssistantRow)
        assert stream._finished  # the stream was flushed, not replaced
        assert stream.query_one(Markdown).source.rstrip("\n") == "".join(FRAGMENTS).rstrip("\n")


@pytest.mark.asyncio
async def test_a_remounted_stream_row_renders_the_text_so_far_and_keeps_streaming():
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _feed(app, pilot, USER, _delta(0, "hello "), _delta(1, "there"))
        await _settle(pilot)
        widget = StreamingMessage("hello there")
        await app.query_one(Conversation).mount(widget)
        widget.append(", friend")
        await _settle(pilot)
        assert widget.query_one(Markdown).source == "hello there, friend"
