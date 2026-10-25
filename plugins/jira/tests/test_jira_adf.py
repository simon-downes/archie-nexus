"""Markdown → ADF conversion and its use by the Jira write functions; no live Jira access."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.jira.adf import markdown_to_adf
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
