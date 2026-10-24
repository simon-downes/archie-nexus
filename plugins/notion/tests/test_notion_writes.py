"""Tests for Notion mutation gates, provider argument shapes and error mapping."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
from archie_plugins.notion.client import NotionToolError, NotionTransportError
from archie_plugins.notion.policy import NotionValidationError, NotionWritePolicyError
from archie_plugins.notion.writes import (
    NotionMutationIndeterminateError,
    NotionWriteError,
    add_comment,
    create_page,
    update_page,
)

ROOT = "11111111-1111-1111-1111-111111111111"
PAGE = "22222222222222222222222222222222"
DATABASE = "2558a35c22c380f8ac26efde57390261"
SOURCE = "2558a35c-22c3-80be-997a-000b8363ec50"
OPEN = {"enabled": True, "scope": None}

# inputSchemas of the write tools as listed by https://mcp.notion.com/mcp (descriptions removed).
SCHEMAS = json.loads((Path(__file__).parent / "fixtures" / "write_tool_schemas.json").read_text())


def _envelope(kind: str, text: str = "", url: str = "") -> dict:
    return {"metadata": {"type": kind}, "title": "T", "url": url, "text": text}


def _state(title: str = "Name") -> str:
    schema = {title: {"name": title, "type": "title"}, "Status": {"type": "status", "groups": {}}}
    return (
        f'<data-source url="{{{{collection://{SOURCE}}}}}">'
        f"<data-source-state>{json.dumps({'url': f'collection://{SOURCE}', 'schema': schema})}"
        "</data-source-state></data-source>"
    )


PAGE_FETCH = _envelope(
    "page", '<ancestor-path><parent-page url="https://app.notion.com/p/' + ROOT + '"/>'
)
DATABASE_FETCH = _envelope(
    "database",
    '<ancestor-path><parent-page url="https://app.notion.com/p/'
    + ROOT
    + '"/></ancestor-path>'
    + _state(),
    url=f"https://app.notion.com/p/{DATABASE}?pvs=204",
)
SOURCE_FETCH = _envelope(
    "data_source", _state(), url=f"https://app.notion.com/p/{DATABASE}?pvs=204"
)


def _provider(responses: dict[str, object], created: object = None):
    """A notion_call fake: fetches return responses[id], writes return ``created``."""

    async def call(name, arguments):
        if name == "notion-fetch":
            return responses[arguments["id"]]
        jsonschema.validate(arguments, SCHEMAS[name])
        return {"pages": [{"id": "new"}]} if created is None else created

    return AsyncMock(side_effect=call)


def _patched(policy, call):
    return (
        patch("archie_plugins.notion.writes.require_write", return_value=policy),
        patch("archie_plugins.notion.writes.notion_call", new=call),
    )


def _write_args(call, name):
    return [c.args[1] for c in call.await_args_list if c.args[0] == name]


async def test_create_page_under_page_sends_pages_list_with_title():
    call = _provider({PAGE: PAGE_FETCH})
    a, b = _patched(OPEN, call)
    with a, b:
        await create_page(PAGE, title=" New ", content="Body")
    assert _write_args(call, "notion-create-pages") == [
        {
            "parent": {"page_id": PAGE},
            "pages": [{"properties": {"title": "New"}, "content": "Body"}],
        }
    ]


async def test_create_page_in_database_uses_data_source_and_title_property():
    call = _provider({DATABASE: DATABASE_FETCH})
    a, b = _patched(OPEN, call)
    with a, b:
        await create_page(
            f"https://www.notion.so/{DATABASE}",
            title="Task",
            properties={"Status": "To Do", "date:Due:start": "2026-11-01"},
        )
    assert _write_args(call, "notion-create-pages") == [
        {
            "parent": {"data_source_id": SOURCE},
            "pages": [
                {"properties": {"Status": "To Do", "date:Due:start": "2026-11-01", "Name": "Task"}}
            ],
        }
    ]


async def test_create_page_accepts_collection_url_parent():
    call = _provider({f"collection://{SOURCE}": SOURCE_FETCH})
    a, b = _patched(OPEN, call)
    with a, b:
        await create_page(f"collection://{SOURCE}", title="Task")
    assert _write_args(call, "notion-create-pages")[0]["parent"] == {"data_source_id": SOURCE}
    assert _write_args(call, "notion-create-pages")[0]["pages"][0]["properties"] == {"Name": "Task"}


async def test_create_page_rejects_database_with_several_data_sources():
    other = DATABASE_FETCH["text"].replace(
        "</data-source>",
        "</data-source>" + _state().replace(SOURCE, "3558a35c-22c3-80be-997a-000b8363ec50"),
    )
    call = _provider({DATABASE: {**DATABASE_FETCH, "text": other}})
    a, b = _patched(OPEN, call)
    with a, b:
        with pytest.raises(NotionValidationError, match="collection://"):
            await create_page(DATABASE, title="Task")
    assert not _write_args(call, "notion-create-pages")


@pytest.mark.parametrize(
    "scope",
    [
        {"pages": set(), "databases": {DATABASE}},
        {"pages": {ROOT.replace("-", "")}, "databases": set()},
    ],
)
async def test_create_page_data_source_parent_is_in_database_scope(scope):
    call = _provider({f"collection://{SOURCE}": SOURCE_FETCH, DATABASE: DATABASE_FETCH})
    a, b = _patched({"enabled": True, "scope": scope}, call)
    with a, b:
        await create_page(f"collection://{SOURCE}", title="Task")
    assert len(_write_args(call, "notion-create-pages")) == 1


async def test_create_page_out_of_scope_database_is_refused():
    call = _provider({DATABASE: DATABASE_FETCH})
    scope = {"pages": {PAGE}, "databases": {"33333333333333333333333333333333"}}
    a, b = _patched({"enabled": True, "scope": scope}, call)
    with a, b:
        with pytest.raises(NotionWriteError):
            await create_page(DATABASE, title="Task")
    assert not _write_args(call, "notion-create-pages")


async def test_create_page_denied_before_mutation():
    with (
        patch(
            "archie_plugins.notion.writes.require_write",
            side_effect=NotionWritePolicyError("disabled"),
        ),
        patch("archie_plugins.notion.writes.notion_call", new=AsyncMock()) as call,
    ):
        with pytest.raises(NotionWritePolicyError):
            await create_page(ROOT, title="New")
    call.assert_not_awaited()


def _failing(responses, error):
    async def call(name, arguments):
        if name == "notion-fetch":
            return responses[arguments["id"]]
        raise error

    return AsyncMock(side_effect=call)


WRITES = [
    (lambda: create_page(PAGE, title="New"), "creation"),
    (lambda: update_page(PAGE, properties={"Status": "Done"}), "update"),
    (lambda: add_comment(PAGE, "hello"), "comment"),
]


@pytest.mark.parametrize(("write", "_label"), WRITES)
async def test_provider_rejection_is_definite_and_keeps_message(write, _label):
    message = "Input validation error: pages: Invalid input"
    a, b = _patched(OPEN, _failing({PAGE: PAGE_FETCH}, NotionToolError(message)))
    with a, b:
        with pytest.raises(NotionToolError, match=f"^{message}$"):
            await write()


@pytest.mark.parametrize(("write", "label"), WRITES)
async def test_transport_failure_is_indeterminate(write, label):
    error = NotionTransportError("Notion MCP request failed")
    a, b = _patched(OPEN, _failing({PAGE: PAGE_FETCH}, error))
    with a, b:
        with pytest.raises(NotionMutationIndeterminateError, match=label) as info:
            await write()
    assert info.value.__cause__ is error


@pytest.mark.parametrize(("write", "_label"), WRITES)
async def test_malformed_write_response_is_indeterminate(write, _label):
    a, b = _patched(OPEN, _provider({PAGE: PAGE_FETCH}, created=["unexpected"]))
    with a, b:
        with pytest.raises(NotionMutationIndeterminateError):
            await write()


async def test_update_requires_a_change():
    with patch("archie_plugins.notion.writes.require_write", return_value=OPEN):
        with pytest.raises(Exception, match="requires"):
            await update_page(ROOT)


async def test_update_properties_shape():
    call = _provider({})
    a, b = _patched(OPEN, call)
    with a, b:
        await update_page(PAGE, properties={"Section": "Platform", "Archived": "__YES__"})
    assert _write_args(call, "notion-update-page") == [
        {
            "page_id": PAGE,
            "command": "update_properties",
            "properties": {"Section": "Platform", "Archived": "__YES__"},
        }
    ]


async def test_update_replacements_map_to_content_updates():
    call = _provider({})
    a, b = _patched(OPEN, call)
    with a, b:
        await update_page(
            PAGE,
            replacements=[{"old": "a", "new": "b"}, {"old": "c", "new": "", "replace_all": True}],
        )
    assert _write_args(call, "notion-update-page") == [
        {
            "page_id": PAGE,
            "command": "update_content",
            "content_updates": [
                {"old_str": "a", "new_str": "b"},
                {"old_str": "c", "new_str": "", "replace_all_matches": True},
            ],
        }
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"replacements": [{"old": "a"}]},
        {"replacements": [{"old": "a", "new": "b", "extra": 1}]},
        {"properties": {"A": "b"}, "replacements": [{"old": "a", "new": "b"}]},
    ],
)
async def test_update_rejects_bad_replacements_before_calling(kwargs):
    call = _provider({})
    a, b = _patched(OPEN, call)
    with a, b:
        with pytest.raises(NotionValidationError):
            await update_page(PAGE, **kwargs)
    call.assert_not_awaited()


async def test_comment_payload_is_bounded_and_explicit():
    call = _provider({}, created={"id": "comment"})
    a, b = _patched(OPEN, call)
    with a, b:
        await add_comment(ROOT, "hello")
    assert _write_args(call, "notion-create-comment") == [
        {
            "page_id": ROOT.replace("-", ""),
            "rich_text": [{"type": "text", "text": {"content": "hello"}}],
        }
    ]


async def test_long_comment_is_split_into_provider_sized_text_items():
    call = _provider({}, created={"id": "comment"})
    a, b = _patched(OPEN, call)
    with a, b:
        await add_comment(PAGE, "x" * 4500)
    items = _write_args(call, "notion-create-comment")[0]["rich_text"]
    assert [len(item["text"]["content"]) for item in items] == [2000, 2000, 500]
