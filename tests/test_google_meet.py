import pytest
from archie_agent.exec.tools import get_all_tools, meet
from archie_agent.exec.tools.google_common import GoogleValidationError


async def test_meet_exact_surface_and_recording_has_no_content(monkeypatch):
    tools = get_all_tools()
    assert {"meet." + name for name in meet.__all__} <= tools.keys()
    monkeypatch.setattr(meet, "credential", lambda: type("C", (), {"access_token": "t"})())

    async def fake_request(*args):
        return [{"state": "available", "content": b"secret", "url": "https://meet.google.com/x"}]

    monkeypatch.setattr(meet, "meet_request", fake_request)
    result = await meet.recording("meet:abc")
    assert result["content"] is None
    assert result["url"].startswith("https://")


async def test_calendar_reference_resolves_before_artifact_fetch(monkeypatch):
    monkeypatch.setattr(meet, "credential", lambda: type("C", (), {"access_token": "t"})())
    calendar_event = {
        "id": "event-1",
        "start": "2026-09-23T08:30:00+00:00",
        "end": "2026-09-23T09:30:00+00:00",
        "meet_url": "https://meet.google.com/abc-defg-hij",
    }
    calls = []

    async def fake_event(event_id, policy_snapshot=None):
        assert event_id == "event-1"
        return calendar_event

    async def fake_request(operation, payload, token):
        calls.append((operation, payload))
        if operation == "records":
            return [
                {
                    "name": "conferenceRecords/other",
                    "startTime": "2026-09-22T08:30:00+00:00",
                    "endTime": "2026-09-22T09:30:00+00:00",
                },
                {
                    "name": "conferenceRecords/record-1",
                    "startTime": "2026-09-23T08:30:00+00:00",
                    "endTime": "2026-09-23T09:30:00+00:00",
                },
            ]
        return [{"state": "available", "content": "Notes", "url": None}]

    monkeypatch.setattr("archie_agent.exec.tools.calendar.get_event", fake_event)
    monkeypatch.setattr(meet, "meet_request", fake_request)
    result = await meet.notes("calendar:event-1")
    assert result["state"] == "available"
    assert result["content"] == "Notes"
    assert calls[0][0] == "records"
    assert calls[0][1]["filter"] == 'space.meeting_code = "abc-defg-hij"'
    assert calls[1] == ("notes", {"record": "conferenceRecords/record-1"})


async def test_smart_notes_reads_google_doc_destination(monkeypatch):
    monkeypatch.setattr(meet, "credential", lambda: type("C", (), {"access_token": "t"})())

    async def fake_request(operation, payload, token):
        if operation == "records":
            return [
                {
                    "name": "conferenceRecords/record-1",
                    "startTime": "2026-09-23T08:30:00+00:00",
                    "endTime": "2026-09-23T09:30:00+00:00",
                }
            ]
        return [
            {
                "state": "FILE_GENERATED",
                "docsDestination": {
                    "document": "doc-1",
                    "exportUri": "https://docs.google.com/document/d/doc-1/view",
                },
            }
        ]

    async def fake_event(event_id, policy_snapshot=None):
        return {
            "id": event_id,
            "start": "2026-09-23T08:30:00+00:00",
            "end": "2026-09-23T09:30:00+00:00",
            "meet_url": "https://meet.google.com/abc-defg-hij",
        }

    async def fake_read_reference(file_id, purpose, policy_snapshot=None):
        assert file_id == "doc-1"
        assert purpose == "read"
        return {
            "content": "Platform notes",
            "file": {"mime_type": "application/vnd.google-apps.document"},
        }

    monkeypatch.setattr("archie_agent.exec.tools.calendar.get_event", fake_event)
    monkeypatch.setattr(meet, "meet_request", fake_request)
    monkeypatch.setattr("archie_agent.exec.tools.drive.read_reference", fake_read_reference)
    result = await meet.notes("calendar:event-1")
    assert result == {
        "state": "available",
        "content": "Platform notes",
        "url": "https://docs.google.com/document/d/doc-1/view",
        "provenance": "google.meet",
    }


async def test_calendar_reference_rejects_tied_recurring_records(monkeypatch):
    async def fake_event(event_id, policy_snapshot=None):
        return {
            "id": event_id,
            "start": "2026-09-23T08:30:00+00:00",
            "end": "2026-09-23T09:30:00+00:00",
            "meet_url": "https://meet.google.com/abc-defg-hij",
        }

    async def fake_request(operation, payload, token):
        if operation == "records":
            return [
                {"name": "conferenceRecords/a", "startTime": "2026-09-23T08:30:00+00:00"},
                {"name": "conferenceRecords/b", "startTime": "2026-09-23T08:30:00+00:00"},
            ]
        return []

    monkeypatch.setattr("archie_agent.exec.tools.calendar.get_event", fake_event)
    monkeypatch.setattr(meet, "meet_request", fake_request)
    with pytest.raises(GoogleValidationError, match="multiple Meet conferences"):
        await meet.notes("calendar:event-1")


async def test_meet_rejects_noncanonical_id_before_provider(monkeypatch):
    called = False
    monkeypatch.setattr(meet, "meet_request", lambda *args: globals().__setitem__("called", True))
    with pytest.raises(GoogleValidationError):
        await meet.notes("free text")
    assert called is False
