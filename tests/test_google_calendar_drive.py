from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from archie_agent.exec.tools import calendar, drive, get_all_tools
from archie_agent.exec.tools.google_common import GoogleValidationError


async def test_calendar_drive_exact_surfaces():
    tools = get_all_tools()
    assert {"calendar." + name for name in calendar.__all__} <= tools.keys()
    assert {"drive." + name for name in drive.__all__} <= tools.keys()


async def test_calendar_bounds_before_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(calendar, "calendar_request", lambda *args: calls.append(args))
    with pytest.raises(GoogleValidationError):
        await calendar.upcoming(days=32)
    assert calls == []


async def test_calendar_normalizes_google_times_and_organizer(monkeypatch):
    monkeypatch.setattr(calendar, "credential", lambda: type("C", (), {"access_token": "t"})())
    monkeypatch.setattr(
        calendar,
        "calendar_request",
        AsyncMock(
            return_value=[
                {
                    "id": "event-1",
                    "summary": "Planning",
                    "start": {"dateTime": "2026-09-24T09:30:00+01:00", "timeZone": "Europe/London"},
                    "end": {"dateTime": "2026-09-24T10:00:00+01:00", "timeZone": "Europe/London"},
                    "organizer": {"email": "organizer@example.com"},
                    "conferenceData": {
                        "entryPoints": [
                            {"entryPointType": "video", "uri": "https://meet.google.com/abc"}
                        ]
                    },
                }
            ]
        ),
    )
    result = await calendar.upcoming(days=1, limit=1)
    assert result[0]["start"] == "2026-09-24T09:30:00+01:00"
    assert result[0]["end"] == "2026-09-24T10:00:00+01:00"
    assert result[0]["organizer"] == "organizer@example.com"
    assert result[0]["meet_url"] == "https://meet.google.com/abc"


async def test_calendar_today_uses_uk_calendar_day(monkeypatch):
    monkeypatch.setattr(
        calendar,
        "_now",
        lambda: datetime(2026, 9, 23, 23, 30, tzinfo=ZoneInfo("Europe/London")),
    )
    monkeypatch.setattr(calendar, "credential", lambda: type("C", (), {"access_token": "t"})())
    request = AsyncMock(return_value=[])
    monkeypatch.setattr(calendar, "calendar_request", request)
    await calendar.today()
    payload = request.await_args.args[1]
    assert payload["timeMin"] == "2026-09-23T00:00:00+01:00"
    assert payload["timeMax"] == "2026-09-24T00:00:00+01:00"


async def test_calendar_search_supports_past_date_and_shared_shape(monkeypatch):
    monkeypatch.setattr(calendar, "credential", lambda: type("C", (), {"access_token": "t"})())
    request = AsyncMock(
        return_value=[
            {
                "id": "past-1",
                "summary": "Planning",
                "description": "Discuss roadmap",
                "start": {"dateTime": "2026-09-21T09:00:00+01:00"},
                "end": {"dateTime": "2026-09-21T10:00:00+01:00"},
                "attendees": [
                    {"email": "a@example.com", "displayName": "A", "responseStatus": "accepted"}
                ],
                "location": "Room A",
            }
        ]
    )
    monkeypatch.setattr(calendar, "calendar_request", request)
    result = await calendar.search(
        "2026-09-21T00:00:00+01:00",
        "2026-09-22T00:00:00+01:00",
        query="roadmap",
        limit=50,
    )
    assert result[0]["description"] == "Discuss roadmap"
    assert result[0]["attendees"] == [
        {"email": "a@example.com", "display_name": "A", "response_status": "accepted"}
    ]
    assert result[0]["location"] == "Room A"
    assert set(result[0]) == {
        "id",
        "summary",
        "description",
        "start",
        "end",
        "status",
        "organizer",
        "attendees",
        "location",
        "meet_url",
        "provenance",
    }


async def test_drive_read_exports_google_doc_content(monkeypatch):
    monkeypatch.setattr(drive, "credential", lambda: type("C", (), {"access_token": "t"})())
    calls = []

    async def fake_request(operation, payload, token):
        calls.append((operation, payload))
        if operation == "metadata":
            return {
                "id": "doc-1",
                "name": "Notes",
                "mimeType": "application/vnd.google-apps.document",
            }
        if operation == "export":
            return "Meeting notes\nNext steps"
        raise AssertionError(operation)

    monkeypatch.setattr(drive, "drive_request", fake_request)
    result = await drive.read("doc-1", format="text")
    assert result["content"] == "Meeting notes\nNext steps"
    assert result["format"] == "text"
    assert result["truncated"] is False
    assert calls == [
        ("metadata", {"id": "doc-1", "purpose": "read"}),
        ("export", {"id": "doc-1", "mime_type": "application/vnd.google-apps.document"}),
    ]


async def test_drive_format_validation_before_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(drive, "drive_request", lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        await drive.read("f1", format="binary")
    assert calls == []
