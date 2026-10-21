"""Tests for Notion database queries and comments."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.notion.policy import NotionPolicyError, NotionValidationError
from archie_plugins.notion.query import NotionQueryError, get_comments, query_database

ROOT = "11111111-1111-1111-1111-111111111111"


async def test_query_authorizes_database_before_rows():
    schema = {
        "id": ROOT,
        "type": "database",
        "ancestors": [],
        "data_sources": ["collection://source-1"],
    }
    rows = {"results": [{"id": "row-1", "Status": "Done"}]}
    call = AsyncMock(side_effect=[schema, rows])
    with patch("archie_plugins.notion.query.notion_call", call):
        result = await query_database(ROOT, limit=1)
    assert result == rows["results"]
    assert call.await_args_list[0].args[0] == "notion-fetch"
    assert call.await_args_list[1].args[0] == "notion-query-data-sources"
    assert call.await_args_list[1].args[1] == {
        "data": {"mode": "rows", "data_source_url": "collection://source-1", "limit": 1}
    }


async def test_query_sends_view_payload_without_data_source():
    schema = {
        "id": ROOT,
        "type": "database",
        "ancestors": [],
        "views": [{"url": "view://view-1"}],
    }
    call = AsyncMock(side_effect=[schema, {"results": []}])
    with patch("archie_plugins.notion.query.notion_call", call):
        await query_database(ROOT, view="view://view-1", limit=2)
    assert call.await_args_list[1].args[1] == {
        "data": {"mode": "view", "view_url": "view://view-1", "page_size": 2}
    }


STATUS_DONE = {
    "type": "property",
    "property": " Status",
    "propertyType": "status",
    "operator": "status_is",
    "value": {"type": "is_option", "value": "Done"},
}

SOURCE = "collection://source-1"
PROPERTIES = {
    " Status": {
        "type": "status",
        "options": ["To Do", "Done"],
        "groups": {"To-do": ["To Do"], "Complete": ["Done"]},
    },
    "Name": {"type": "title"},
    "Tag": {"type": "select", "options": ["Now", "Next"]},
    "Created": {"type": "created_time"},
}
TYPED_SCHEMA = {
    "id": ROOT,
    "type": "database",
    "ancestors": [],
    "data_sources": [SOURCE],
    "schemas": {SOURCE: {"name": "Tasks", "properties": PROPERTIES}},
}


def _group(*filters, operator="and"):
    return {"type": "group", "operator": operator, "filters": list(filters)}


def _leaf(prop, prop_type, operator, value=None):
    leaf = {"type": "property", "property": prop, "propertyType": prop_type, "operator": operator}
    if value is not None:
        leaf["value"] = value
    return leaf


async def test_query_sends_structured_rows_options():
    group = _group(STATUS_DONE, operator="or")
    call = AsyncMock(side_effect=[TYPED_SCHEMA, {"results": []}])
    with patch("archie_plugins.notion.query.notion_call", call):
        await query_database(
            ROOT,
            filter=group,
            sort=[{"property": "Created", "direction": "descending"}],
        )
    assert call.await_args_list[1].args[1] == {
        "data": {
            "mode": "rows",
            "data_source_url": SOURCE,
            "limit": 50,
            "filter": group,
            "sort": [{"property": "Created", "direction": "descending"}],
        }
    }


@pytest.mark.parametrize(
    "good",
    [
        _group(_leaf(" Status", "status", "status_is", {"type": "is_group", "value": "to-do"})),
        _group(_leaf(" Status", "status", "status_is", {"type": "is_option", "value": "done"})),
        _group(_leaf("Tag", "select", "enum_is", [{"type": "exact", "value": "Now"}])),
        _group(_leaf("Name", "title", "is_not_empty")),
        _group(
            _group(
                _leaf("Tag", "select", "enum_is", {"type": "exact", "value": "Now"}),
                _leaf("Tag", "select", "enum_is", {"type": "exact", "value": "Next"}),
                operator="or",
            ),
            _leaf("Name", "title", "string_contains", {"type": "exact", "value": "a"}),
        ),
    ],
)
async def test_query_accepts_filters_matching_the_schema(good):
    call = AsyncMock(side_effect=[TYPED_SCHEMA, {"results": []}])
    with patch("archie_plugins.notion.query.notion_call", call):
        await query_database(ROOT, filter=good)
    assert call.await_args_list[1].args[1]["data"]["filter"] == good


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (_group(_leaf("Status", "status", "is_empty")), "'Status' is not a property.*' Status'"),
        (_group(_leaf(" Status", "select", "is_empty")), "has propertyType 'status', not 'select'"),
        (
            _group(_leaf("Tag", "select", "enum_is", {"type": "exact", "value": "now"})),
            "'now' is not an option of 'Tag'.*case-sensitive",
        ),
        (
            _group(_leaf(" Status", "status", "status_is", {"type": "is_group", "value": "to_do"})),
            "not a status group.*'To-do'",
        ),
        (
            _group(
                _leaf(" Status", "status", "status_is", {"type": "is_option", "value": "Doing"})
            ),
            "'Doing' is not an option",
        ),
        (_group(_group(_group(STATUS_DONE))), "nest only one level"),
        (_group({"property": " Status", "status": {"equals": "Done"}}), '"type": "property"'),
    ],
)
async def test_query_rejects_filters_that_do_not_match_the_schema(bad, message):
    call = AsyncMock(return_value=TYPED_SCHEMA)
    with patch("archie_plugins.notion.query.notion_call", call):
        with pytest.raises(NotionValidationError, match=message):
            await query_database(ROOT, filter=bad)
    assert call.await_count == 1


async def test_query_rejects_sort_on_unknown_property_and_keeps_spaces():
    call = AsyncMock(return_value=TYPED_SCHEMA)
    with patch("archie_plugins.notion.query.notion_call", call):
        with pytest.raises(NotionValidationError, match="'Status' is not a property"):
            await query_database(ROOT, sort=[{"property": "Status", "direction": "ascending"}])
    call = AsyncMock(side_effect=[TYPED_SCHEMA, {"results": []}])
    with patch("archie_plugins.notion.query.notion_call", call):
        await query_database(ROOT, sort=[{"property": " Status", "direction": "ascending"}])
    assert call.await_args_list[1].args[1]["data"]["sort"][0]["property"] == " Status"


async def test_query_with_filter_fails_closed_without_schema():
    schema = {key: value for key, value in TYPED_SCHEMA.items() if key != "schemas"}
    call = AsyncMock(return_value=schema)
    with patch("archie_plugins.notion.query.notion_call", call):
        with pytest.raises(NotionQueryError, match="cannot be checked"):
            await query_database(ROOT, filter=_group(STATUS_DONE))
    assert call.await_count == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"or": [{"property": "Status", "select": {"equals": "Done"}}]},
        STATUS_DONE,
        {"type": "group", "operator": "xor", "filters": [STATUS_DONE]},
        {"type": "group", "operator": "and", "filters": []},
        {"type": "group", "operator": "and", "filters": STATUS_DONE},
        "Status = Done",
    ],
)
async def test_query_rejects_filter_without_outer_group(bad):
    schema = {
        "id": ROOT,
        "type": "database",
        "ancestors": [],
        "data_sources": ["collection://source-1"],
    }
    call = AsyncMock(return_value=schema)
    with patch("archie_plugins.notion.query.notion_call", call):
        with pytest.raises(NotionValidationError, match="filter must be a group"):
            await query_database(ROOT, filter=bad)
    assert call.await_count == 1


async def test_query_rejects_filter_with_view():
    schema = {
        "id": ROOT,
        "type": "database",
        "ancestors": [],
        "views": [{"url": "view://view-1"}],
    }
    group = {"type": "group", "operator": "and", "filters": [STATUS_DONE]}
    call = AsyncMock(return_value=schema)
    with patch("archie_plugins.notion.query.notion_call", call):
        with pytest.raises(NotionValidationError, match="cannot be combined with view"):
            await query_database(ROOT, view="view://view-1", filter=group)
    assert call.await_count == 1


async def test_query_denies_database_before_row_request():
    schema = {
        "id": ROOT,
        "type": "database",
        "ancestors": [],
        "data_sources": ["collection://source-1"],
    }
    call = AsyncMock(return_value=schema)
    with (
        patch("archie_plugins.notion.query.notion_call", call),
        patch(
            "archie_plugins.notion.query.require_read",
            return_value={
                "enabled": True,
                "scope": {"pages": set(), "databases": {"22222222222222222222222222222222"}},
            },
        ),
    ):
        with pytest.raises(NotionPolicyError):
            await query_database(ROOT)
    assert call.await_count == 1


async def test_get_comments_authorizes_page_before_comments():
    page = {"id": ROOT, "type": "page", "ancestors": []}
    comments = {"results": [{"id": "comment-1", "text": "hello"}]}
    call = AsyncMock(side_effect=[page, comments])
    with patch("archie_plugins.notion.query.notion_call", call):
        result = await get_comments(ROOT, limit=1)
    assert result == comments["results"]
    assert call.await_args_list[1].args[0] == "notion-get-comments"
