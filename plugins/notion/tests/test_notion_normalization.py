"""Tests for live Notion MCP envelope normalization."""

import json
from pathlib import Path

from archie_plugins.notion.normalization import normalize_comments, normalize_fetch

FIXTURES = Path(__file__).parent / "fixtures"

PAGE = "3de8a35c22c380c580def0f67737abc7"
PARENT = "2798a35c22c380fab63bcfb22110c4d5"


def test_normalize_fetch_envelope_extracts_page_content_and_ancestry():
    raw = {
        "metadata": {"type": "page"},
        "title": "Archie Test",
        "url": f"https://app.notion.com/p/{PAGE}",
        "text": (
            f'<page url="https://app.notion.com/p/{PAGE}">'
            f'<ancestor-path><parent-page url="https://app.notion.com/p/{PARENT}" title="Scratch"/>'
            '</ancestor-path><properties>\n{"title":"Archie Test"}\n</properties>'
            "<content>\nThis is the test space for archie\n</content></page>"
        ),
        "path": "Simon’s Scratch Pad",
        "page_last_edited_at": "2026-09-17T18:31:14.403Z",
    }
    result = normalize_fetch(raw, PAGE)
    assert result["id"] == "3de8a35c-22c3-80c5-80de-f0f67737abc7"
    assert result["ancestors"] == ["2798a35c-22c3-80fa-b63b-cfb22110c4d5"]
    assert result["content"] == "This is the test space for archie"
    assert result["properties"] == {"title": "Archie Test"}


def test_normalize_fetch_extracts_wrapped_database_urls():
    raw = {
        "metadata": {"type": "database"},
        "text": (
            '<data-sources><data-source url="{{collection://source-1}}">'
            '<views><view url="{{view://view-1}}"></view></views>'
        ),
    }
    result = normalize_fetch(raw, PAGE)
    assert result["data_sources"] == ["collection://source-1"]
    assert result["views"] == [{"url": "view://view-1"}]


STATE = {
    "name": "Tasks Database",
    "url": "collection://source-1",
    "schema": {
        " Status": {
            "name": " Status",
            "type": "status",
            "groups": {
                "complete": [{"name": "Done", "url": "collectionPropertyOption://x/1"}],
                "current": [],
                "in_progress": [{"name": "In progress", "url": "collectionPropertyOption://x/2"}],
                "to_do": [{"name": "To Do", "url": "collectionPropertyOption://x/3"}],
            },
        },
        "Tag": {
            "name": "Tag",
            "type": "select",
            "options": [{"color": "pink", "name": "Now", "url": "collectionPropertyOption://x/4"}],
        },
        "Due": {"name": "Due", "type": "date", "querySqlColumns": {"columns": []}},
        "Broken": {"name": "Broken"},
    },
}


def test_normalize_fetch_extracts_data_source_schema():
    raw = {
        "metadata": {"type": "database"},
        "text": (
            '<data-sources><data-source url="{{collection://source-1}}">\n'
            f"<data-source-state>\n{json.dumps(STATE)}\n</data-source-state>\n"
            "<sqlite-table>...</sqlite-table></data-source></data-sources>"
        ),
    }
    result = normalize_fetch(raw, PAGE)
    assert result["schemas"] == {
        "collection://source-1": {
            "name": "Tasks Database",
            "properties": {
                " Status": {
                    "type": "status",
                    "options": ["Done", "In progress", "To Do"],
                    "groups": {
                        "To-do": ["To Do"],
                        "In progress": ["In progress"],
                        "Complete": ["Done"],
                    },
                },
                "Tag": {"type": "select", "options": ["Now"]},
                "Due": {"type": "date"},
            },
        }
    }


def test_normalize_fetch_skips_unparseable_schema():
    raw = {
        "metadata": {"type": "database"},
        "text": "<data-source-state>\n{not json}\n</data-source-state>",
    }
    assert "schemas" not in normalize_fetch(raw, PAGE)


def test_normalize_comments_empty_suggested_edits_response():
    assert normalize_comments({"suggested_edits_status": "not_enabled"}) == []


def test_normalize_comments_results_envelope():
    assert normalize_comments({"results": [{"id": "comment", "text": "hello"}]}) == [
        {"id": "comment", "text": "hello"}
    ]


def test_normalize_comments_xml_discussion():
    raw = {
        "text": '<discussions total-count="1"><comment id="comment-1" url="page?d=1" user-url="user://simon" datetime="2026-09-17T18:35:02Z">Commenting on a test</comment></discussions>'
    }
    assert normalize_comments(raw) == [
        {
            "id": "comment-1",
            "url": "page?d=1",
            "user_url": "user://simon",
            "created": "2026-09-17T18:35:02Z",
            "body": "Commenting on a test",
        }
    ]


def test_normalize_comments_live_response_with_extra_attributes():
    # Sanitised capture of notion-get-comments; the by-you attribute broke the old parser.
    raw = json.loads((FIXTURES / "get_comments_response.json").read_text())
    assert normalize_comments(raw) == [
        {
            "id": "4008a35c-22c3-8104-84d0-001d2d98910e",
            "url": "https://app.notion.com/p/4008a35c22c381f1a3faf4a6e81d1f25"
            "?d=4008a35c22c381059721001c3202f66a&pvs=42",
            "user_url": "user://00000000-0000-0000-0000-000000000000/user@example.com",
            "created": "2026-10-21T20:22:58.752Z",
            "body": "Email from Test, 21 Oct: test comment. https://mail.google.com/mail/u/0/#all/test",
        }
    ]


def test_normalize_comments_threads_with_replies_in_order():
    def comment(n, body, extra=""):
        return (
            f'<comment id="c{n}" url="https://app.notion.com/p/x?d=d{n}&amp;pvs=42" '
            f'user-url="user://u{n}" datetime="2026-10-21T20:2{n}:00.000Z"{extra}>{body}</comment>'
        )

    raw = {
        "suggested_edits_status": "not_enabled",
        "text": (
            '<discussions total-count="2" shown-count="2">\n'
            '<discussion id="discussion://p/p/d1" comment-count="2" resolved="false" '
            'type="comment" context="page">\n'
            + comment(1, "First")
            + "\n"
            + comment(2, "Reply &amp; more\nsecond line", ' by-you="true"')
            + '\n</discussion>\n<discussion id="discussion://p/b/d2" comment-count="1" '
            'resolved="false" type="comment" context="block">\n'
            + comment(3, "On a block")
            + "\n</discussion>\n</discussions>"
        ),
    }
    result = normalize_comments(raw)
    assert [item["id"] for item in result] == ["c1", "c2", "c3"]
    assert result[1]["body"] == "Reply & more\nsecond line"
    assert result[0]["url"] == "https://app.notion.com/p/x?d=d1&pvs=42"
    assert set(result[0]) == {"id", "url", "user_url", "created", "body"}


def test_normalize_comments_empty_discussions():
    raw = {"suggested_edits_status": "not_enabled", "text": '<discussions total-count="0"/>'}
    assert normalize_comments(raw) == []
