"""Markdown → ADF conversion and its use by the Jira write functions; no live Jira access."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.jira.adf import ALLOWED, markdown_to_adf
from archie_plugins.jira.issues import JiraInputError, add_comment, create_issue, update_issue

WRITE = {"jira": {"write": {"enabled": True, "scope": ["PLAT"]}}}


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


BR = {"type": "hardBreak"}


def para(*content):
    return {"type": "paragraph", "content": list(content)}


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def _client(*responses):
    client = AsyncMock()
    client.request.side_effect = list(responses)
    return client


# --- Tracer: lines and paragraphs -----------------------------------------------------------


def test_multiline_text_keeps_lines_and_paragraphs():
    assert markdown_to_adf("a\nb\n\nc") == doc(para(text("a"), BR, text("b")), para(text("c")))


def test_crlf_matches_lf_and_trailing_newlines_are_ignored():
    assert markdown_to_adf("a\r\nb\r\n\r\nc\r\n\r\n") == markdown_to_adf("a\nb\n\nc")


def test_single_line_and_unicode():
    assert markdown_to_adf("héllo — 日本 😀") == doc(para(text("héllo — 日本 😀")))


def test_raw_html_is_literal_text():
    assert markdown_to_adf("<b>x</b>") == doc(para(text("<b>x</b>")))
    assert markdown_to_adf("<div>\nq\n</div>") == doc(
        para(text("<div>"), BR, text("q"), BR, text("</div>"))
    )


def test_empty_input_is_an_empty_document():
    assert markdown_to_adf("") == doc()


# --- Rich Markdown --------------------------------------------------------------------------

INLINE = {"text", "hardBreak"}
MARKS = {"strong", "em", "strike", "code", "link"}


def validate(node, parent="doc"):
    """Fail if a produced node breaks the ADF nesting and mark rules the plan lists."""
    kind = node["type"]
    content = node.get("content", [])
    if kind == "doc":
        assert node["version"] == 1
    if kind in ALLOWED:
        for child in content:
            assert child["type"] in ALLOWED[kind] or (kind == "doc" and child["type"] == "table"), (
                f"{child['type']} inside {kind}"
            )
        if kind != "doc":
            assert content, f"empty {kind}"
    if kind == "listItem":
        assert content[0]["type"] in {"paragraph", "codeBlock"}
    if kind in {"bulletList", "orderedList"}:
        assert content and all(child["type"] == "listItem" for child in content)
    if kind == "table":
        assert parent in {"doc"} and content
        assert all(row["type"] == "tableRow" for row in content)
    if kind == "tableRow":
        assert content and all(c["type"] in {"tableCell", "tableHeader"} for c in content)
    if kind in {"paragraph", "heading"}:
        assert all(child["type"] in INLINE for child in content)
    if kind == "heading":
        assert 1 <= node["attrs"]["level"] <= 6
    if kind == "codeBlock":
        assert all(c["type"] == "text" and "marks" not in c for c in content)
    if kind == "panel":
        assert node["attrs"]["panelType"] in {"info", "note", "warning", "success", "error"}
    if kind == "text":
        assert node["text"], "empty text node"
        types = [mark["type"] for mark in node.get("marks", [])]
        assert set(types) <= MARKS and len(types) == len(set(types))
        if "code" in types:
            assert set(types) <= {"code", "link"}
    for child in content:
        validate(child, kind)


def test_headings_and_marks():
    assert markdown_to_adf("## Title **b**\n\n*i* ~~s~~ `c` ***bi***") == doc(
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [text("Title "), text("b", {"type": "strong"})],
        },
        para(
            text("i", {"type": "em"}),
            text(" "),
            text("s", {"type": "strike"}),
            text(" "),
            text("c", {"type": "code"}),
            text(" "),
            text("bi", {"type": "strong"}, {"type": "em"}),
        ),
    )


def test_links_images_and_code_marks():
    link = {"type": "link", "attrs": {"href": "https://x.test", "title": "T"}}
    assert markdown_to_adf('[a **b**](https://x.test "T")') == doc(
        para(text("a ", link), text("b", {"type": "strong"}, link))
    )
    # code keeps only link; empty link text shows the URL; images become links.
    assert markdown_to_adf(
        "**[`c`](https://l.test)** [](https://u.test) ![alt](https://i.test)"
    ) == doc(
        para(
            text("c", {"type": "code"}, {"type": "link", "attrs": {"href": "https://l.test"}}),
            text(" "),
            text("https://u.test", {"type": "link", "attrs": {"href": "https://u.test"}}),
            text(" "),
            text("alt", {"type": "link", "attrs": {"href": "https://i.test"}}),
        )
    )
    assert markdown_to_adf("<https://a.test>") == doc(
        para(text("https://a.test", {"type": "link", "attrs": {"href": "https://a.test"}}))
    )


def item(*content):
    return {"type": "listItem", "content": list(content)}


def test_nested_and_ordered_lists():
    assert markdown_to_adf("- a\n  - b\n- c\n\n7. x\n8. y") == doc(
        {
            "type": "bulletList",
            "content": [
                item(para(text("a")), {"type": "bulletList", "content": [item(para(text("b")))]}),
                item(para(text("c"))),
            ],
        },
        {
            "type": "orderedList",
            "attrs": {"order": 7},
            "content": [item(para(text("x"))), item(para(text("y")))],
        },
    )
    assert markdown_to_adf("0. z")["content"][0]["attrs"] == {"order": 0}
    assert "attrs" not in markdown_to_adf("1. z")["content"][0]


def test_empty_list_items_get_an_empty_paragraph():
    assert markdown_to_adf("-\n-\n  - x") == doc(
        {
            "type": "bulletList",
            "content": [
                item(para()),
                item(para(), {"type": "bulletList", "content": [item(para(text("x")))]}),
            ],
        }
    )


def test_code_blocks_quotes_and_rules():
    assert markdown_to_adf("```python extra\nx = 1\n  y\n```\n\n    indented\n\n---\n\n> q") == doc(
        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("x = 1\n  y")]},
        {"type": "codeBlock", "content": [text("indented")]},
        {"type": "rule"},
        {"type": "blockquote", "content": [para(text("q"))]},
    )
    assert markdown_to_adf("```\n```") == doc({"type": "codeBlock", "content": []})


def cell(kind, *content):
    return {"type": kind, "content": list(content) or [para()]}


def test_tables_have_header_cells_and_padded_rows():
    result = markdown_to_adf("| a | b |\n|---|---|\n| 1 |\n| `x` | |")
    assert result == doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        cell("tableHeader", para(text("a"))),
                        cell("tableHeader", para(text("b"))),
                    ],
                },
                {
                    "type": "tableRow",
                    "content": [cell("tableCell", para(text("1"))), cell("tableCell", para())],
                },
                {
                    "type": "tableRow",
                    "content": [
                        cell("tableCell", para(text("x", {"type": "code"}))),
                        cell("tableCell", para()),
                    ],
                },
            ],
        }
    )


@pytest.mark.parametrize(
    ("keyword", "panel_type"),
    [
        ("NOTE", "info"),
        ("TIP", "success"),
        ("IMPORTANT", "note"),
        ("WARNING", "warning"),
        ("caution", "error"),
    ],
)
def test_alerts_become_panels(keyword, panel_type):
    assert markdown_to_adf(f"> [!{keyword}]\n> Body **b**\n>\n> - i") == doc(
        {
            "type": "panel",
            "attrs": {"panelType": panel_type},
            "content": [
                para(text("Body "), text("b", {"type": "strong"})),
                {"type": "bulletList", "content": [item(para(text("i")))]},
            ],
        }
    )


def test_alert_edge_cases():
    assert markdown_to_adf("> [!TIP]") == doc(
        {"type": "panel", "attrs": {"panelType": "success"}, "content": [para()]}
    )
    # A marker followed by text on the same line is not an alert (GitHub rule).
    assert markdown_to_adf("> [!NOTE] x")["content"][0]["type"] == "blockquote"
    assert markdown_to_adf("> [!OTHER]\n> x")["content"][0]["type"] == "blockquote"


def test_disallowed_nesting_degrades():
    strong = {"type": "strong"}
    # Heading in a list item becomes a bold paragraph; a quote in a quote is spliced in.
    assert markdown_to_adf("- a\n\n  # h\n\n> > deep") == doc(
        {"type": "bulletList", "content": [item(para(text("a")), para(text("h", strong)))]},
        {"type": "blockquote", "content": [para(text("deep"))]},
    )
    # A table in a list becomes one paragraph per row.
    table_in_list = markdown_to_adf("- x\n\n  | a | b |\n  |---|---|\n  | 1 | 2 |")
    assert table_in_list["content"][0]["content"][0]["content"][1:] == [
        para(text("a | b")),
        para(text("1 | 2")),
    ]
    # Code and a rule in a panel: code becomes code-marked lines, the rule is dropped.
    assert markdown_to_adf("> [!NOTE]\n> ```\n> a\n> b\n> ```\n> ***") == doc(
        {
            "type": "panel",
            "attrs": {"panelType": "info"},
            "content": [para(text("a", {"type": "code"}), BR, text("b", {"type": "code"}))],
        }
    )


CORPUS = [
    "a\nb\n\nc",
    "# H1\n## H2 `code`\n###### H6",
    "*i* **b** ~~s~~ `c` [l](https://x.test) ![i](https://i.test)",
    "- a\n  - b\n    - c\n- d\n\n3. x\n4. y",
    "```js\nlet x;\n```\n\n    indented\n\n---",
    "> quote\n>\n> - item\n>\n> ```\n> code\n> ```",
    "| h1 | h2 |\n|----|----|\n| **a** | `b` |\n| c |",
    "> [!WARNING]\n> careful\n>\n> ## heading in panel",
    "- [ ] task\n- [x] done",
    "- a\n\n  > q\n\n  | t |\n  |---|\n  | 1 |\n\n  # h\n\n  > [!NOTE]\n  > n",
    "> > > a\n> > > ```\n> > > c\n> > > ```",
    "<b>raw</b> & &amp; \\* snake_case __init__ 1. #x [x] |p|",
]


@pytest.mark.parametrize("source", CORPUS)
def test_corpus_produces_valid_adf(source):
    validate(markdown_to_adf(source))


def test_deep_and_mixed_nesting_never_raises():
    sources = [
        "> " * 50 + "x",
        "".join("  " * level + "- l\n" for level in range(50)),
        "- > - > - > # h\n  > | a |\n  > |---|\n  > | 1 |",
        "> [!NOTE]\n> > [!TIP]\n> > - ```\n> >   c\n> >   ```",
        "[" * 500 + "x" + "]" * 500,
        "*" * 1000,
        "\x00\t\v\f",
    ]
    for source in sources:
        validate(markdown_to_adf(source))


def _texts(node):
    return (
        [node["text"]]
        if node["type"] == "text"
        else [t for child in node.get("content", []) for t in _texts(child)]
    )


@pytest.mark.parametrize(
    "source",
    ["> " * 150 + "deep text", "- " * 150 + "deep text", "> - " * 16000 + "deep text"],
)
def test_nesting_past_parser_limit_falls_back_to_plain_lines(source):
    result = markdown_to_adf(source)
    validate(result)
    assert any("deep text" in value for value in _texts(result))


def test_nesting_within_limit_keeps_structure():
    result = markdown_to_adf("> " * 40 + "deep")
    validate(result)
    assert result["content"][0]["type"] == "blockquote"
    assert _texts(result) == ["deep"]


# --- Write functions ------------------------------------------------------------------------


async def test_create_issue_sends_structured_description(policy):
    policy(WRITE)
    client = _client({"key": "PLAT-1", "id": "1"})
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await create_issue("PLAT", "s", description="one\ntwo\n\nthree")
    fields = client.request.call_args_list[0].kwargs["json"]["fields"]
    assert fields["description"] == doc(para(text("one"), BR, text("two")), para(text("three")))


async def test_create_issue_omits_whitespace_only_description(policy):
    policy(WRITE)
    client = _client({"key": "PLAT-1", "id": "1"})
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await create_issue("PLAT", "s", description="  \n")
    assert "description" not in client.request.call_args_list[0].kwargs["json"]["fields"]


@pytest.mark.parametrize(
    ("description", "expected"),
    [(" \n ", None), ("a\nb", doc(para(text("a"), BR, text("b"))))],
)
async def test_update_issue_description(description, expected, policy):
    policy(WRITE)
    client = _client(None, {"id": "1", "key": "PLAT-1", "fields": {}})
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await update_issue("PLAT-1", description=description)
    assert client.request.call_args_list[0].kwargs["json"]["fields"] == {"description": expected}


async def test_add_comment_sends_structured_body(policy):
    policy(WRITE)
    client = _client({"id": "9", "body": doc()})
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await add_comment("PLAT-1", "x\ny")
    assert client.request.call_args_list[0].kwargs["json"]["body"] == doc(
        para(text("x"), BR, text("y"))
    )


async def test_add_comment_rejects_blank_body_before_any_request(policy):
    policy(WRITE)
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        with pytest.raises(JiraInputError):
            await add_comment("PLAT-1", " \n ")
    factory.assert_not_called()


async def test_body_limit_is_jira_cap(policy):
    policy(WRITE)
    client = _client({"key": "PLAT-1", "id": "1"}, {"id": "9", "body": doc()})
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await create_issue("PLAT", "s", description="d" * 32767)
        await add_comment("PLAT-1", "c" * 32767)
    assert client.request.call_count == 2


@pytest.mark.parametrize(
    "call",
    [
        lambda: create_issue("PLAT", "s", description="d" * 32768),
        lambda: update_issue("PLAT-1", description="d" * 32768),
        lambda: add_comment("PLAT-1", "c" * 32768),
    ],
)
async def test_over_limit_is_rejected_before_any_request(call, policy):
    policy(WRITE)
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        with pytest.raises(JiraInputError, match="32767"):
            await call()
    factory.assert_not_called()
