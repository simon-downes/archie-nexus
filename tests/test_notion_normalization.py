"""Tests for live Notion MCP envelope normalization."""

from archie_agent.exec.tools.notion.normalization import normalize_comments, normalize_fetch

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
            "</ancestor-path><properties>\n{\"title\":\"Archie Test\"}\n</properties>"
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
