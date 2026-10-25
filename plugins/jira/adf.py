"""Markdown <-> Atlassian Document Format (ADF) for Jira descriptions and comments.

The writer parses CommonMark plus GFM tables and strikethrough with markdown-it-py, builds an
ADF tree, then normalises it so every node sits only where ADF allows it (Jira rejects
invalid ADF with a 400). Constructs ADF cannot nest are degraded rather than rejected.

The reader renders ADF as Markdown the writer reads back to the same ADF. Jira-only nodes
(expands, mentions, media, statuses, task lists) become readable text.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
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

_LANGUAGE = re.compile(r"[A-Za-z0-9_+#.-]{1,40}")

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
            if stack[-1]["type"] == "heading":
                # An ATX heading is one line, so a setext heading's line breaks become spaces.
                inline = [
                    {"type": "text", "text": " "} if n["type"] == "hardBreak" else n for n in inline
                ]
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
    _finish(root)
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
    root = {"type": "doc", "version": 1, "content": content}
    _finish(root)
    root["content"] = [node for node in root["content"] if node["content"]]
    return root


def _code_block(token: Token) -> dict:
    node: dict[str, Any] = {"type": "codeBlock"}
    words = token.info.split() if token.info else []
    # Only plain language names survive a round trip (a "|" would turn the fence into a table).
    language = words[0] if words and _LANGUAGE.fullmatch(words[0]) else ""
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
            href = str(token.attrs.get("href", ""))
            attrs = {"href": href or "#"}
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


_EMPHASIS = {"strong", "em", "strike"}


def _canonical(nodes: list[dict]) -> list[dict]:
    """Put inline content in the one form the reader writes back.

    Whitespace at line edges and breaks at paragraph edges are dropped (Jira does not show
    them). Emphasis never starts or ends on whitespace, because Markdown cannot express that.
    Adjacent text nodes with equal marks are merged.
    """
    nodes = _tidy([dict(node) for node in nodes])
    split: list[dict] = []
    for index, node in enumerate(nodes):
        marks = node.get("marks", [])
        names = {m["type"] for m in marks} & _EMPHASIS
        before = nodes[index - 1] if index else None
        after = nodes[index + 1] if index + 1 < len(nodes) else None
        if node["type"] != "text" or not names:
            split.append(node)
            continue
        if not node["text"].strip(" \t"):
            kept = [m for m in _keep(marks, before) if m in _keep(marks, after)]
            split.append(_text_node(node["text"], kept))
            continue
        core = node["text"].strip(" \t")
        start = node["text"].index(core)
        lead, tail = node["text"][:start], node["text"][start + len(core) :]
        if lead:
            split.append(_text_node(lead, _keep(marks, before)))
        split.append({**node, "text": core})
        if tail:
            split.append(_text_node(tail, _keep(marks, after)))
    merged: list[dict] = []
    for node in split:
        last = merged[-1] if merged else None
        if (
            last
            and node["type"] == last["type"] == "text"
            and node.get("marks", []) == last.get("marks", [])
        ):
            merged[-1] = {**last, "text": last["text"] + node["text"]}
        else:
            merged.append(node)
    return merged


def _keep(marks: list[dict], neighbour: dict | None) -> list[dict]:
    """Marks edge whitespace may keep: non-emphasis marks, and emphasis the neighbour shares."""
    shared = (
        {m["type"] for m in (neighbour or {}).get("marks", [])}
        if neighbour and neighbour["type"] == "text"
        else set()
    )
    return [m for m in marks if m["type"] not in _EMPHASIS or m["type"] in shared]


def _text_node(value: str, marks: list[dict]) -> dict:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def _finish(node: dict) -> None:
    if node["type"] in {"paragraph", "heading"}:
        node["content"] = _canonical(node["content"])
    else:
        for child in node.get("content", []):
            if child["type"] != "text":
                _finish(child)


def _tidy(nodes: list[dict]) -> list[dict]:
    for index, node in enumerate(nodes):
        if node["type"] != "text" or any(m["type"] == "code" for m in node.get("marks", [])):
            continue
        if index == len(nodes) - 1 or nodes[index + 1]["type"] == "hardBreak":
            node["text"] = node["text"].rstrip(" \t")
        if index == 0 or nodes[index - 1]["type"] == "hardBreak":
            node["text"] = node["text"].lstrip(" \t")
    nodes = [node for node in nodes if node["type"] != "text" or node["text"]]
    while nodes and nodes[0]["type"] == "hardBreak":
        nodes.pop(0)
    while nodes and nodes[-1]["type"] == "hardBreak":
        nodes.pop()
    return nodes


_CODE = {"type": "code"}


def _text_length(result: list[dict]) -> int:
    return sum(len(node.get("text", "")) for node in result)


def _append(result: list[dict], value: str, marks: list[dict]) -> None:
    if not value:
        return
    unique: dict[str, dict] = {}
    for mark in marks:
        unique[mark["type"]] = dict(mark)  # nested "*a *b* c*" repeats a mark; ADF forbids it
    ordered = sorted(unique.values(), key=lambda m: _MARK_ORDER[m["type"]])
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
    # Degradation can leave empty paragraphs; Markdown cannot express them, so drop them.
    placed = [n for n in placed if n["type"] != "paragraph" or n["content"]]
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


# --- ADF -> Markdown ------------------------------------------------------------------------

_PANEL_KEYWORDS = {value: key for key, value in PANELS.items()}
_DELIMITERS = {"strong": "**", "em": "*", "strike": "~~"}
_INLINE_TYPES = {
    "text",
    "hardBreak",
    "mention",
    "emoji",
    "date",
    "status",
    "inlineCard",
    "mediaInline",
    "placeholder",
}


def adf_to_markdown(value: Any) -> str:
    """Render an ADF document (or a plain string) as Markdown. Never raises."""
    if isinstance(value, str):
        return value
    try:
        nodes = value if isinstance(value, list) else [value]
        return _blocks(_dicts(nodes)).strip("\n")
    except RecursionError:
        return " ".join(_all_text(value))


def _dicts(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _content(node: dict) -> list[dict]:
    return _dicts(node.get("content"))


def _attrs(node: dict) -> dict:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _all_text(value: Any) -> list[str]:
    """Iterative text extraction, the last-resort reader for pathological nesting."""
    found, pending = [], [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            if isinstance(item.get("text"), str):
                found.append(item["text"])
            pending.append(item.get("content"))
    return found


# Blocks


def _blocks(nodes: list[dict]) -> str:
    parts: list[str] = []
    previous = None
    alternate = False
    for node in nodes:
        kind = node.get("type")
        # Adjacent lists of one kind merge unless their markers differ, so alternate them.
        alternate = (not alternate) if kind == previous else False
        rendered = _block(node, alternate)
        if rendered.strip():
            parts.append(rendered)
            previous = kind
        elif kind == previous:
            alternate = not alternate
    return "\n\n".join(parts)


def _block(node: dict, alternate: bool = False) -> str:
    kind = node.get("type")
    if kind == "doc":
        return _blocks(_content(node))
    if kind in _INLINE_TYPES or kind is None and "text" in node:
        return _paragraph_text([node])
    if kind == "paragraph":
        return _paragraph_text(_content(node))
    if kind == "heading":
        level = _attrs(node).get("level")
        level = level if isinstance(level, int) and 1 <= level <= 6 else 1
        text = _paragraph_text(_content(node), heading=True)
        # A trailing run of "#" after a space would be read as a closing sequence.
        text = re.sub(r"(^|[ \t])(#+)$", lambda m: m.group(1) + "\\" + m.group(2), text)
        return ("#" * level + " " + text).rstrip()
    if kind == "codeBlock":
        return _fence(node)
    if kind == "rule":
        return "---"
    if kind == "blockquote":
        return _quote(_blocks(_content(node)))
    if kind == "panel":
        keyword = _PANEL_KEYWORDS.get(_attrs(node).get("panelType"))
        body = _blocks(_content(node))
        # A blank line after the marker keeps the body from continuing the marker's paragraph.
        return (
            _quote(f"[!{keyword}]" + (f"\n\n{body}" if body else "")) if keyword else _quote(body)
        )
    if kind in {"bulletList", "orderedList"}:
        return _list(node, alternate)
    if kind in {"taskList", "decisionList"}:
        return _task_list(node)
    if kind == "table":
        return _table(node)
    if kind in {"expand", "nestedExpand"}:
        title = _attrs(node).get("title")
        heading = (
            f"**{_escape(title.strip(), True)}**"
            if isinstance(title, str) and title.strip()
            else ""
        )
        return "\n\n".join(part for part in (heading, _blocks(_content(node))) if part)
    if kind in {"mediaSingle", "mediaGroup"}:
        return "\n".join(_media(child) for child in _content(node))
    if kind == "media":
        return _media(node)
    if kind in {"blockCard", "embedCard"}:
        url = _card_url(node)
        return f"<{url}>" if url else ""
    content = _content(node)
    if content and all(child.get("type") in _INLINE_TYPES for child in content):
        return _paragraph_text(content)
    return _blocks(content)


def _quote(body: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in (body or "").split("\n"))


def _indent(body: str, first: str, rest: str) -> str:
    lines = body.split("\n")
    out = [(first + lines[0]).rstrip()]
    out.extend((rest + line) if line else "" for line in lines[1:])
    return "\n".join(out)


def _list(node: dict, alternate: bool) -> str:
    ordered = node.get("type") == "orderedList"
    start = _attrs(node).get("order") if ordered else None
    number = start if isinstance(start, int) and start >= 0 else 1
    items = []
    for item in _content(node):
        marker = (
            f"{number}{')' if alternate else '.'} " if ordered else ("* " if alternate else "- ")
        )
        number += 1
        items.append(_indent(_item_body(_content(item)), marker, " " * len(marker)))
    return "\n".join(items)


def _item_body(children: list[dict]) -> str:
    parts: list[str] = []
    previous = None
    alternate = False
    for index, child in enumerate(children):
        kind = child.get("type")
        alternate = (not alternate) if kind == previous else False
        rendered = _block(child, alternate)
        if not rendered.strip() and index:
            continue
        if parts:
            # A list may follow its item's paragraph directly, unless its first item is empty:
            # a bare "-" under text would make that text a heading. Other blocks need a gap.
            # Only a list starting at 1 may interrupt a paragraph; an empty one has none.
            interrupts = kind == "bulletList" or (
                kind == "orderedList" and _attrs(child).get("order", 1) == 1
            )
            tight = not parts[-1].strip() or (interrupts and " " in rendered.split("\n")[0])
            parts.append("\n" if tight else "\n\n")
        parts.append(rendered)
        previous = kind
    return "".join(parts)


def _task_list(node: dict) -> str:
    lines = []
    for item in _content(node):
        kind = item.get("type")
        if kind in {"taskList", "decisionList"}:
            lines.append(_indent(_task_list(item), "  ", "  "))
            continue
        children = _content(item)
        inline = all(child.get("type") in _INLINE_TYPES for child in children)
        body = _paragraph_text(children) if inline else _item_body(children)
        if kind in {"taskItem", "blockTaskItem"}:
            box = "[x]" if _attrs(item).get("state") == "DONE" else "[ ]"
            lines.append(_indent(body, f"- {box} ", "      "))
        else:
            lines.append(_indent(body, "- ", "  "))
    return "\n".join(lines)


def _fence(node: dict) -> str:
    body = "".join(child.get("text", "") for child in _content(node) if child.get("type") == "text")
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    language = _attrs(node).get("language")
    info = language if isinstance(language, str) and _LANGUAGE.fullmatch(language) else ""
    return f"{fence}{info}\n{body}\n{fence}" if body else f"{fence}{info}\n{fence}"


def _table(node: dict) -> str:
    rows = [_content(row) for row in _content(node) if row.get("type") == "tableRow"]
    rows = [row for row in rows if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rendered = [[_cell(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
    if all(cell.get("type") == "tableHeader" for cell in rows[0]):
        header, body = rendered[0], rendered[1:]
    else:
        header, body = [""] * width, rendered
    lines = [_row(header), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(_row(row) for row in body)
    return "\n".join(lines)


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _cell(cell: dict) -> str:
    parts: list[str] = []
    for block in _content(cell):
        kind = block.get("type")
        if kind in {"bulletList", "orderedList", "taskList", "decisionList"}:
            parts.extend("- " + _inline_run(_flat(item), table=True) for item in _content(block))
        elif kind == "codeBlock":
            code = "".join(child.get("text", "") for child in _content(block))
            parts.extend(_code_span(line, table=True) for line in code.split("\n") if line)
        else:
            text = _inline_run(_flat(block), table=True)
            if text:
                parts.append(text)
    return "<br>".join(parts)


def _flat(node: dict) -> list[dict]:
    """Inline content of a block and its descendants, with breaks between blocks."""
    if node.get("type") in _INLINE_TYPES:
        return [node]
    result: list[dict] = []
    for child in _content(node):
        part = _flat(child)
        if part and result and child.get("type") not in _INLINE_TYPES:
            result.append({"type": "hardBreak"})
        result.extend(part)
    return result


def _media(node: dict) -> str:
    attrs = _attrs(node)
    name = attrs.get("alt") or attrs.get("filename") or attrs.get("id")
    return f"[attachment: {name}]" if isinstance(name, str) and name else "[attachment]"


def _card_url(node: dict) -> str:
    attrs = _attrs(node)
    url = attrs.get("url")
    if not isinstance(url, str):
        data = attrs.get("data")
        url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) else ""


# Inline


def _paragraph_text(nodes: list[dict], heading: bool = False) -> str:
    text = _inline_run(nodes, heading=heading)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    # An empty line would end the paragraph; a lone backslash keeps it as a line break.
    return "\n".join(line or "\\" for line in lines)


def _split_newlines(nodes: list[dict]) -> list[dict]:
    """Raw newlines inside text nodes (as the old plugin stored them) become hardBreaks."""
    result: list[dict] = []
    for node in nodes:
        if node.get("type") == "text" and "\n" in str(node.get("text", "")):
            for index, part in enumerate(str(node["text"]).split("\n")):
                if index:
                    result.append({"type": "hardBreak"})
                if part:
                    result.append({**node, "text": part})
        else:
            result.append(node)
    return result


def _marks(node: dict) -> list[dict]:
    return _dicts(node.get("marks"))


def _link_of(node: dict) -> tuple[str, str] | None:
    for mark in _marks(node):
        if mark.get("type") == "link":
            attrs = _attrs(mark)
            href = attrs.get("href")
            if isinstance(href, str) and href:
                title = attrs.get("title")
                return href, title if isinstance(title, str) else ""
    return None


def _inline_run(nodes: list[dict], table: bool = False, heading: bool = False) -> str:
    """Render inline nodes as Markdown.

    Each link group (consecutive nodes with the same link) is one atom. Emphasis is then
    applied once over the whole run, so bold around a link stays one bold span.
    """
    nodes = _split_newlines(nodes)
    if heading:
        nodes = [
            {"type": "text", "text": " "} if n.get("type") == "hardBreak" else n for n in nodes
        ]
    segments: list[tuple[list[str], str]] = []  # (emphasis marks, rendered text); "\n" = break
    line_start = True
    index = 0
    while index < len(nodes):
        node = nodes[index]
        if node.get("type") == "hardBreak":
            segments.append(([], "\n"))
            line_start = True
            index += 1
            continue
        link = _link_of(node)
        if not link:
            rendered = _inline_node(node, table, line_start, _is_code(node))
            if rendered:
                segments.append((_emphasis(node), rendered))
                line_start = False
            index += 1
            continue
        end = index + 1
        while (
            end < len(nodes)
            and nodes[end].get("type") != "hardBreak"
            and _link_of(nodes[end]) == link
        ):
            end += 1
        group = nodes[index:end]
        common = [m for m in _emphasis(group[0]) if all(m in _emphasis(n) for n in group)]
        inner = _apply_emphasis(
            [
                (
                    [m for m in _emphasis(n) if m not in common],
                    _inline_node(n, table, False, _is_code(n)),
                )
                for n in group
            ]
        )
        href, title = link
        # Backslashes and entity-like "&" would be decoded on re-read; escape them.
        safe = re.sub(r"&(?=#?\w+;)", r"\\&", href.replace("\\", "\\\\"))
        target = (
            f"<{safe.replace('<', '%3C').replace('>', '%3E')}>"
            if re.search(r"[\s()<>]", safe)
            else safe
        )
        if title:
            target += ' "' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if segments and _unescaped_bang(segments[-1][1]):
            marks, text = segments[-1]
            segments[-1] = (marks, text[:-1] + "\\!")  # "![" would start an image
        segments.append((common, f"[{inner or _escape(href, False)}]({target})"))
        line_start = False
        index = end
    return _apply_emphasis(segments)


def _unescaped_bang(text: str) -> bool:
    if not text.endswith("!"):
        return False
    slashes = len(text[:-1]) - len(text[:-1].rstrip("\\"))
    return slashes % 2 == 0


def _is_code(node: dict) -> bool:
    return node.get("type") == "text" and any(m.get("type") == "code" for m in _marks(node))


def _emphasis(node: dict) -> list[str]:
    names = {m.get("type") for m in _marks(node)} & set(_DELIMITERS)
    if _is_code(node):
        return []  # code takes no other marks in ADF
    return sorted(names, key=lambda name: _MARK_ORDER[name])


def _apply_emphasis(segments: list[tuple[list[str], str]]) -> str:
    """Join rendered segments, opening and closing emphasis delimiters as marks change.

    Whitespace at a span's edge is moved outside its delimiters, because Markdown cannot
    open or close emphasis next to whitespace.
    """
    out: list[str] = []
    stack: list[str] = []
    used: list[str] = []  # the delimiter each open mark was written with
    closed_star = [False]  # the output ends with a "*" closer just written

    def close(keep: int) -> None:
        if len(stack) <= keep:
            return
        text = "".join(out)
        stripped = text.rstrip(" \t")
        out[:] = [stripped]
        while len(stack) > keep:
            stack.pop()
            out.append(used.pop())
        closed_star[0] = not text[len(stripped) :] and out[-1].endswith("*")
        out.append(text[len(stripped) :])

    def delimiter(name: str) -> str:
        # "**a***b*" is ambiguous: a "*" opener straight after a "*" closer merges into one
        # run. Underscores avoid that when the following text cannot make "_" intraword.
        if name in {"strong", "em"} and closed_star[0]:
            return "__" if name == "strong" else "_"
        return _DELIMITERS[name]

    def run_length(name: str, start: int) -> int:
        length = 0
        for marks, text in segments[start:]:
            if text == "\n" or (name not in marks and text.strip()):
                break
            length += 1
        return length

    for position, (wanted, rendered) in enumerate(segments):
        if rendered == "\n":
            close(0)
            out.append("\n")
            closed_star[0] = False
            continue
        if not rendered.strip():
            wanted = [name for name in stack if name in wanted]
        keep = 0
        while keep < len(stack) and stack[keep] in wanted:
            keep += 1
        close(keep)
        # Open the mark that lasts longest outermost, so it need not close and reopen.
        opening = sorted(
            (name for name in wanted if name not in stack),
            key=lambda name: (-run_length(name, position), _MARK_ORDER[name]),
        )
        if opening:
            stripped = rendered.lstrip(" \t")
            out.append(rendered[: len(rendered) - len(stripped)])
            rendered = stripped
            for name in opening:
                mark = delimiter(name)
                out.append(mark)
                used.append(mark)
                stack.append(name)
        out.append(rendered)
        closed_star[0] = False
    close(0)
    return "".join(out)


def _inline_node(node: dict, table: bool, line_start: bool, is_code: bool) -> str:
    kind = node.get("type")
    attrs = _attrs(node)
    if kind == "text" or kind is None:
        value = node.get("text")
        if not isinstance(value, str):
            return _inline_run(_content(node), table) if _content(node) else ""
        return _code_span(value, table) if is_code else _escape(value, line_start)
    if kind == "mention":
        text = attrs.get("text")
        text = text if isinstance(text, str) and text else "@user"
        return _escape(text if text.startswith("@") else "@" + text, line_start)
    if kind == "emoji":
        text = attrs.get("text") or attrs.get("shortName") or ""
        return _escape(str(text), line_start)
    if kind == "date":
        try:
            stamp = datetime.fromtimestamp(int(attrs.get("timestamp")) / 1000, UTC)
            return stamp.date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return _escape(str(attrs.get("timestamp", "")), line_start)
    if kind == "status":
        text = attrs.get("text")
        return f"[{_escape(text, False)}]" if isinstance(text, str) and text else ""
    if kind == "inlineCard":
        url = _card_url(node)
        return f"<{url}>" if url and not re.search(r"[\s<>]", url) else _escape(url, line_start)
    if kind == "mediaInline":
        return _media(node)
    return _inline_run(_content(node), table) if _content(node) else ""


_LINE_START = re.compile(
    r"(?P<hash>#{1,6}(?=[ \t]|$))"
    r"|(?P<bullet>[-+](?=[ \t]|$))"
    r"|(?P<ordered>\d{1,9}(?=[.)](?:[ \t]|$)))"
    r"|(?P<quote>>)"
    r"|(?P<setext>(?:-+|=+)[ \t]*$)"
)


def _escape(value: str, line_start: bool) -> str:
    """Escape inline text so the writer reads back exactly this text (plan D12)."""
    out = []
    for index, char in enumerate(value):
        before = value[index - 1] if index else ""
        after = value[index + 1] if index + 1 < len(value) else ""
        if char in "\\*`[]|~":
            out.append("\\" + char)
        elif char == "_" and not (before.isalnum() and after.isalnum()):
            out.append("\\_")
        elif char == "<" and (after.isalpha() or after == "/"):
            out.append("\\<")
        elif char == "&" and re.match(
            r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);", value[index:]
        ):
            out.append("\\&")
        else:
            out.append(char)
    text = "".join(out)
    if line_start:
        text = text.lstrip(" \t")
        match = _LINE_START.match(text)
        if match:
            if match.group("ordered"):
                cut = match.end()
                text = text[:cut] + "\\" + text[cut:]
            else:
                text = "\\" + text
    return text


def _code_span(value: str, table: bool = False) -> str:
    value = value.replace("\n", " ")
    if table:
        value = value.replace("|", "\\|")
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    ticks = "`" * (longest + 1)
    pad = (
        value.startswith("`")
        or value.endswith("`")
        or (value.startswith(" ") and value.endswith(" ") and value.strip())
    )
    return f"{ticks} {value} {ticks}" if pad else f"{ticks}{value}{ticks}"
