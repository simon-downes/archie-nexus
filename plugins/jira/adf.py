"""Markdown to Atlassian Document Format (ADF) conversion for Jira descriptions and comments."""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

# html off keeps raw HTML as literal text; Jira has no HTML node.
_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def markdown_to_adf(text: str) -> dict:
    """Convert Markdown to an ADF document. Never raises for string input."""
    source = text.replace("\r\n", "\n").replace("\r", "\n")
    root: dict[str, Any] = {"type": "doc", "version": 1, "content": []}
    stack: list[dict[str, Any]] = [root]
    for token in _MD.parse(source):
        if token.type == "paragraph_open":
            node = {"type": "paragraph", "content": []}
            stack[-1]["content"].append(node)
            stack.append(node)
        elif token.type == "paragraph_close":
            stack.pop()
        elif token.type == "inline":
            inline = _inline(token.children or [])
            if stack[-1]["type"] == "paragraph":
                stack[-1]["content"].extend(inline)
            else:
                stack[-1]["content"].append({"type": "paragraph", "content": inline})
        elif token.type in {"fence", "code_block"} and token.content:
            stack[-1]["content"].append(_paragraph_lines(token.content.rstrip("\n")))
    return root


def _paragraph_lines(value: str) -> dict:
    content: list[dict] = []
    for index, line in enumerate(value.split("\n")):
        if index:
            content.append({"type": "hardBreak"})
        if line:
            content.append({"type": "text", "text": line})
    return {"type": "paragraph", "content": content}


def _inline(tokens: list[Token]) -> list[dict]:
    result: list[dict] = []
    for token in tokens:
        if token.type in {"softbreak", "hardbreak"}:
            result.append({"type": "hardBreak"})
        elif token.content:
            _append_text(result, token.content)
    return result


def _append_text(result: list[dict], value: str) -> None:
    last = result[-1] if result else None
    if last and last["type"] == "text" and not last.get("marks"):
        last["text"] += value
    else:
        result.append({"type": "text", "text": value})
