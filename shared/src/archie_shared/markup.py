"""Escaping and parsing for the Rich markup subset used in tool summaries.

Tool summaries are Rich console markup (``[bold]…[/]``). The TUI renders them with Rich;
the web viewer and plain-text labels use :func:`parse`, which reads markup the way Rich
does. Escaping and parsing live together so they stay consistent, and the tests check
both against Rich itself.

Rich's backslash rules (``rich.markup``):

- A tag is ``[`` followed by a letter, ``#``, ``/`` or ``@``, up to the next ``]`` with no
  ``[`` in between. A run of backslashes before a tag is halved into literal backslashes,
  and an odd one left over makes the tag literal text.
- Elsewhere, ``\\[`` becomes ``[`` and every other backslash is literal. ``]`` needs no
  escaping.

So ``\\[slug\\]`` in user text must not become ``\\\\[slug\\]``: Rich reads that as a
literal backslash and a style named ``slug\\``, which Textual fails to render.
"""

from __future__ import annotations

import re

# rich.markup.RE_TAGS: a run of backslashes, then a tag.
_TAG = re.compile(r"((\\*)\[([a-z#/@][^[]*?)])")
# A "[" (with the backslashes before it) that Rich would read as the start of a tag.
_BRACKET = re.compile(r"(\\*)\[")
_TAG_BODY = re.compile(r"[a-z#/@][^[]*?]")
_TRAILING_BACKSLASHES = re.compile(r"\\+$")


def escape(text: str, *, tag_follows: bool = True) -> str:
    """Escape ``text`` so Rich shows it literally.

    With ``tag_follows`` (the default, for ``[style]{text}[/]``), a trailing run of
    backslashes is doubled so it doesn't escape the closing tag. Pass ``False`` when plain
    text or the end of the markup follows, where the run is already literal.
    """

    def bracket(match: re.Match[str]) -> str:
        run = match.group(1)
        if _TAG_BODY.match(text, match.end()):
            return run * 2 + "\\["  # tag-like: halve-able run, then an escaping backslash
        return run + "\\["  # Rich turns "\[" into "[" and keeps the run as is

    escaped = _BRACKET.sub(bracket, text)
    if not tag_follows:
        return escaped
    return _TRAILING_BACKSLASHES.sub(lambda m: m.group(0) * 2, escaped)


def parse(markup: str) -> list[tuple[str, tuple[str, ...]]]:
    """Split markup into ``(text, styles)`` runs as Rich reads it.

    ``styles`` holds the open tags' contents, outermost first. An unmatched closing tag is
    ignored, where Rich would raise. Adjacent runs with the same styles are merged.
    """
    runs: list[tuple[str, tuple[str, ...]]] = []
    stack: list[str] = []

    def add(text: str) -> None:
        text = text.replace("\\[", "[")
        if not text:
            return
        styles = tuple(stack)
        if runs and runs[-1][1] == styles:
            runs[-1] = (runs[-1][0] + text, styles)
        else:
            runs.append((text, styles))

    position = 0
    for match in _TAG.finditer(markup):
        full, escapes, tag = match.groups()
        if match.start() > position:
            add(markup[position : match.start()])
        position = match.end()
        if escapes:
            literal, escaped = divmod(len(escapes), 2)
            if literal:
                add("\\" * literal)
            if escaped:
                add(full[len(escapes) :])
                continue
        if tag.startswith("/"):
            name = tag[1:].strip().lower()
            if not name:
                if stack:
                    stack.pop()
                continue
            for index in range(len(stack) - 1, -1, -1):
                if stack[index].strip().lower() == name:
                    del stack[index]
                    break
        else:
            stack.append(tag)
    if position < len(markup):
        add(markup[position:])
    return runs


def plain_text(markup: str) -> str:
    """The text Rich would show for ``markup``, without styles."""
    return "".join(text for text, _ in parse(markup))
