"""Markdown to Atlassian Document Format (ADF) conversion for Jira descriptions and comments.

The writer parses CommonMark plus GFM tables and strikethrough with markdown-it-py, builds an
ADF tree, then normalises it so every node sits only where ADF allows it (Jira rejects
invalid ADF with a 400). Constructs ADF cannot nest are degraded rather than rejected.
"""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

# Container depth markdown-it will parse. Past it the parser silently drops content, so
# deeper input falls back to plain lines; far past it the parser itself overflows the stack.
_MAX_NESTING = 100
# html off keeps raw HTML as literal text; Jira has no HTML node.
_MD = MarkdownIt("commonmark", {"html": False, "maxNesting": _MAX_NESTING}).enable(
    ["table", "strikethrough"]
)

# Block children each container may hold (Atlassian ADF node reference).
ALLOWED: dict[str, frozenset[str]] = {
    "doc": frozenset(
        {"paragraph", "heading", "bulletList", "orderedList", "codeBlock", "blockquote"}
        | {"panel", "rule", "table"}
    ),
    "listItem": frozenset({"paragraph", "bulletList", "orderedList", "codeBlock"}),
    "blockquote": frozenset({"paragraph", "bulletList", "orderedList", "codeBlock"}),
    "panel": frozenset({"paragraph", "heading", "bulletList", "orderedList"}),
    "tableCell": frozenset(
        {"paragraph", "heading", "bulletList", "orderedList", "codeBlock", "blockquote"}
        | {"panel", "rule"}
    ),
}
ALLOWED["tableHeader"] = ALLOWED["tableCell"]

# GitHub alert keyword -> ADF panelType.
PANELS = {
    "NOTE": "info",
    "TIP": "success",
    "IMPORTANT": "note",
    "WARNING": "warning",
    "CAUTION": "error",
}
_ALERT = re.compile(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(?:\n|$)", re.IGNORECASE)

_MARK_ORDER = {"strong": 0, "em": 1, "strike": 2, "code": 3, "link": 4}

_BLOCK_OPEN = {
    "paragraph_open": "paragraph",
    "bullet_list_open": "bulletList",
    "ordered_list_open": "orderedList",
    "list_item_open": "listItem",
    "blockquote_open": "blockquote",
    "table_open": "table",
    "tr_open": "tableRow",
    "th_open": "tableHeader",
    "td_open": "tableCell",
}
_BLOCK_CLOSE = {name.replace("_open", "_close") for name in _BLOCK_OPEN} | {"heading_close"}


def markdown_to_adf(text: str) -> dict:
    """Convert Markdown to a valid ADF document. Never raises for string input."""
    source = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        tokens = _MD.parse(source)
    except RecursionError:
        tokens = None
    if tokens is None or _truncated(tokens):
        return _plain(source)
    root: dict[str, Any] = {"type": "doc", "version": 1, "content": []}
    raw: dict[int, str] = {}  # id(paragraph) -> its source text, for alert detection
    stack: list[dict[str, Any]] = [root]
    for token in tokens:
        kind = token.type
        if kind in _BLOCK_OPEN:
            node: dict[str, Any] = {"type": _BLOCK_OPEN[kind], "content": []}
            if kind == "ordered_list_open":
                start = token.attrs.get("start")
                if isinstance(start, int) and start != 1:
                    node["attrs"] = {"order": start}
            stack[-1]["content"].append(node)
            stack.append(node)
        elif kind == "heading_open":
            node = {"type": "heading", "attrs": {"level": int(token.tag[1])}, "content": []}
            stack[-1]["content"].append(node)
            stack.append(node)
        elif kind in _BLOCK_CLOSE:
            stack.pop()
        elif kind == "inline":
            inline = _inline(token.children or [])
            if stack[-1]["type"] in {"paragraph", "heading"}:
                stack[-1]["content"].extend(inline)
                raw[id(stack[-1])] = token.content
            else:  # table cells hold inline content directly
                stack[-1]["content"].append({"type": "paragraph", "content": inline})
        elif kind in {"fence", "code_block"}:
            stack[-1]["content"].append(_code_block(token))
        elif kind == "hr":
            stack[-1]["content"].append({"type": "rule"})
    root["content"] = _children(root, raw)
    return root


def _truncated(tokens: list[Token]) -> bool:
    """True when markdown-it hit its nesting limit and dropped a container's contents.

    Only quotes and list items re-enter the block parser, which is where the limit applies.
    """
    return any(
        token.type in {"blockquote_open", "list_item_open"} and token.level >= _MAX_NESTING - 1
        for token in tokens
    )


def _plain(source: str) -> dict:
    """Fallback: blank-line-separated paragraphs with line breaks, no Markdown."""
    content = []
    for block in re.split(r"\n[ \t]*\n", source.strip("\n")):
        lines = block.split("\n")
        inline: list[dict] = []
        for index, line in enumerate(lines):
            if index:
                inline.append({"type": "hardBreak"})
            if line:
                inline.append({"type": "text", "text": line})
        if inline:
            content.append(_paragraph(inline))
    return {"type": "doc", "version": 1, "content": content}


def _code_block(token: Token) -> dict:
    node: dict[str, Any] = {"type": "codeBlock"}
    language = token.info.strip().split(" ")[0] if token.info else ""
    if language:
        node["attrs"] = {"language": language}
    body = token.content[:-1] if token.content.endswith("\n") else token.content
    node["content"] = [{"type": "text", "text": body}] if body else []
    return node


# --- Inline ---------------------------------------------------------------------------------


def _inline(tokens: list[Token]) -> list[dict]:
    result: list[dict] = []
    marks: list[dict] = []
    link_starts: list[int] = []
    for token in tokens:
        kind = token.type
        if kind in {"softbreak", "hardbreak"}:
            result.append({"type": "hardBreak"})
        elif kind in {"strong_open", "em_open", "s_open"}:
            marks.append({"type": {"strong_open": "strong", "em_open": "em"}.get(kind, "strike")})
        elif kind == "link_open":
            attrs = {"href": str(token.attrs.get("href", ""))}
            if token.attrs.get("title"):
                attrs["title"] = str(token.attrs["title"])
            marks.append({"type": "link", "attrs": attrs})
            link_starts.append(_text_length(result))
        elif kind in {"strong_close", "em_close", "s_close", "link_close"}:
            wanted = {"strong_close": "strong", "em_close": "em", "s_close": "strike"}.get(
                kind, "link"
            )
            for index in range(len(marks) - 1, -1, -1):
                if marks[index]["type"] == wanted:
                    mark = marks.pop(index)
                    start = link_starts.pop() if wanted == "link" and link_starts else None
                    if start is not None and start == _text_length(result):
                        # "[](url)" has no text; keep the URL visible instead of losing it.
                        _append(result, mark["attrs"]["href"], [*marks, mark])
                    break
        elif kind == "code_inline":
            # ADF allows code only alongside link.
            _append(result, token.content, [m for m in marks if m["type"] == "link"] + [_CODE])
        elif kind == "image":
            alt = "".join(child.content for child in token.children or []) or str(
                token.attrs.get("src", "")
            )
            link = {"type": "link", "attrs": {"href": str(token.attrs.get("src", ""))}}
            _append(result, alt, [m for m in marks if m["type"] != "link"] + [link])
        elif token.content:
            _append(result, token.content, marks)
    return result


_CODE = {"type": "code"}


def _text_length(result: list[dict]) -> int:
    return sum(len(node.get("text", "")) for node in result)


def _append(result: list[dict], value: str, marks: list[dict]) -> None:
    if not value:
        return
    ordered = sorted((dict(m) for m in marks), key=lambda m: _MARK_ORDER[m["type"]])
    last = result[-1] if result else None
    if last and last["type"] == "text" and last.get("marks", []) == ordered:
        last["text"] += value
        return
    node: dict[str, Any] = {"type": "text", "text": value}
    if ordered:
        node["marks"] = ordered
    result.append(node)


# --- Normalisation --------------------------------------------------------------------------


def _children(container: dict, raw: dict[int, str]) -> list[dict]:
    """Normalise a container's children so each is allowed there; returns the new list."""
    kind = container["type"]
    placed: list[dict] = []
    for child in container.get("content", []):
        placed.extend(_place(child, kind, raw))
    if kind == "listItem" and (not placed or placed[0]["type"] not in {"paragraph", "codeBlock"}):
        placed.insert(0, _paragraph([]))
    if kind in {"blockquote", "panel", "tableCell", "tableHeader"} and not placed:
        placed.append(_paragraph([]))
    return placed


def _place(node: dict, container: str, raw: dict[int, str]) -> list[dict]:
    kind = node["type"]
    if kind == "blockquote":
        node = _alert(node, raw)
        kind = node["type"]
    if kind in {"bulletList", "orderedList"}:
        for item in node["content"]:
            item["content"] = _children(item, raw)
    elif kind in {"blockquote", "panel"}:
        node["content"] = _children(node, raw)
    elif kind == "table":
        for row in node["content"]:
            for cell in row["content"]:
                cell["content"] = _children(cell, raw)
    if kind in ALLOWED[container]:
        return [node]
    # Degrade nodes ADF does not allow in this container (plan D8).
    if kind == "heading":
        return [_paragraph(_with_strong(node["content"]))]
    if kind in {"blockquote", "panel"}:
        return [grand for child in node["content"] for grand in _place(child, container, raw)]
    if kind == "table":
        return [_paragraph(_row_inline(row)) for row in node["content"]]
    if kind == "codeBlock":
        return [_paragraph(_code_lines(node))]
    return []  # rule: no allowed degradation keeps meaning, so it is dropped


def _alert(quote: dict, raw: dict[int, str]) -> dict:
    first = quote["content"][0] if quote["content"] else None
    if not first or first["type"] != "paragraph":
        return quote
    match = _ALERT.match(raw.get(id(first), ""))
    if not match:
        return quote
    content = first["content"]
    # Drop the leading "[!KIND]" text and the line break after it.
    head = content[0] if content else None
    if head and head["type"] == "text" and not head.get("marks"):
        marker = re.match(r"\[!\w+\][ \t]*", head["text"])
        rest = head["text"][marker.end() :] if marker else head["text"]
        trimmed = ([{**head, "text": rest}] if rest else []) + content[1:]
        if trimmed and trimmed[0]["type"] == "hardBreak":
            trimmed = trimmed[1:]
        first["content"] = trimmed
    body = [first] if first["content"] else []
    return {
        "type": "panel",
        "attrs": {"panelType": PANELS[match.group(1).upper()]},
        "content": body + quote["content"][1:],
    }


def _paragraph(content: list[dict]) -> dict:
    merged: list[dict] = []
    for node in content:
        last = merged[-1] if merged else None
        if (
            last
            and node["type"] == last["type"] == "text"
            and node.get("marks", []) == last.get("marks", [])
        ):
            merged[-1] = {**last, "text": last["text"] + node["text"]}
        else:
            merged.append(node)
    return {"type": "paragraph", "content": merged}


def _with_strong(content: list[dict]) -> list[dict]:
    result = []
    for node in content:
        marks = node.get("marks", [])
        if node["type"] == "text" and not any(m["type"] in {"code", "strong"} for m in marks):
            ordered = sorted([{"type": "strong"}, *marks], key=lambda m: _MARK_ORDER[m["type"]])
            node = {**node, "marks": ordered}
        result.append(node)
    return result


def _code_lines(node: dict) -> list[dict]:
    body = "".join(child.get("text", "") for child in node.get("content", []))
    result: list[dict] = []
    for index, line in enumerate(body.split("\n")):
        if index:
            result.append({"type": "hardBreak"})
        if line:
            result.append({"type": "text", "text": line, "marks": [_CODE]})
    return result


def _flat_inline(node: dict) -> list[dict]:
    """Inline content of any block, with hardBreaks between its blocks."""
    kind = node["type"]
    if kind in {"paragraph", "heading"}:
        return list(node.get("content", []))
    if kind == "codeBlock":
        return _code_lines(node)
    result: list[dict] = []
    for child in node.get("content", []):
        part = _flat_inline(child)
        if kind in {"bulletList", "orderedList"}:
            part = [{"type": "text", "text": "- "}, *part]
        if part:
            if result:
                result.append({"type": "hardBreak"})
            result.extend(part)
    return result


def _row_inline(row: dict) -> list[dict]:
    result: list[dict] = []
    for index, cell in enumerate(row["content"]):
        if index:
            result.append({"type": "text", "text": " | "})
        result.extend(_flat_inline(cell))
    return result
