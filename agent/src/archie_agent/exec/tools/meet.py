"""Bounded Google Meet artifact references."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from archie_agent.exec.tools import tool
from archie_agent.exec.tools.google_common import (
    GoogleValidationError,
    credential,
    google_json,
    require_read,
    safe_id,
    safe_url,
)


async def meet_request(operation: str, payload: dict[str, Any], token: str) -> Any:
    if operation == "records":
        data = await google_json(
            "https://meet.googleapis.com/v2/conferenceRecords", token, params=payload
        )
        return data.get("conferenceRecords", [])
    record = payload["record"]
    if operation == "notes":
        data = await google_json(
            f"https://meet.googleapis.com/v2/{record}/smartNotes",
            token,
            params={"pageSize": 10},
        )
        return data.get("smartNotes", [])
    if operation == "transcript":
        data = await google_json(
            f"https://meet.googleapis.com/v2/{record}/transcripts",
            token,
            params={"pageSize": 10},
        )
        return data.get("transcripts", [])
    if operation == "recording":
        data = await google_json(
            f"https://meet.googleapis.com/v2/{record}/recordings",
            token,
            params={"pageSize": 10},
        )
        return data.get("recordings", [])
    raise GoogleValidationError("unsupported Meet operation")


def _meet_code(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "meet.google.com",
        "meet.googleusercontent.com",
    }:
        return None
    code = parsed.path.strip("/").split("/", 1)[0]
    return code if code and len(code) <= 64 else None


def _record_time(record: dict[str, Any], field: str) -> datetime | None:
    value = record.get(field)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _select_record(
    records: list[Any], event_start: datetime, event_end: datetime
) -> dict[str, Any]:
    candidates = [record for record in records if isinstance(record, dict)]
    containing = []
    for record in candidates:
        start = _record_time(record, "startTime") or _record_time(record, "start_time")
        end = _record_time(record, "endTime") or _record_time(record, "end_time")
        if start and start <= event_start and (end is None or event_start < end):
            containing.append(record)
    if len(containing) == 1:
        return containing[0]
    if len(containing) > 1:
        raise GoogleValidationError("Calendar event matched multiple Meet conferences")
    dated = []
    for record in candidates:
        start = _record_time(record, "startTime") or _record_time(record, "start_time")
        if start:
            distance = abs((start - event_start).total_seconds())
            if distance <= max((event_end - event_start).total_seconds(), 3600):
                dated.append((distance, record))
    if len(dated) == 1:
        return dated[0][1]
    if len(dated) > 1:
        dated.sort(key=lambda item: item[0])
        if dated[0][0] < dated[1][0]:
            return dated[0][1]
        raise GoogleValidationError("Calendar event matched multiple Meet conferences")
    raise GoogleValidationError("Calendar event did not resolve to a Meet conference")


async def resolve_meeting(
    meeting_id: str, policy_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    meeting_id = safe_id(meeting_id, "meeting_id", maximum=500)
    require_read(policy_snapshot)
    if ":" not in meeting_id:
        raise GoogleValidationError("meeting_id must use a canonical prefix")
    kind, value = meeting_id.split(":", 1)
    if kind == "meet" and value:
        if "/" in value:
            raise GoogleValidationError("meet reference must name a conference record ID")
        return {"kind": kind, "id": value}
    if kind == "calendar" and value:
        from archie_agent.exec.tools.calendar import get_event

        event = await get_event(value, policy_snapshot)
        code = _meet_code(event.get("meet_url"))
        if not code:
            raise GoogleValidationError("Calendar event has no safe Google Meet URL")
        start = event.get("start")
        end = event.get("end")
        params = {"filter": f'space.meeting_code = "{code}"', "pageSize": 10}
        if not isinstance(start, str) or not isinstance(end, str):
            raise GoogleValidationError("Calendar event has invalid Meet time bounds")
        try:
            event_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            event_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            raise GoogleValidationError("Calendar event has invalid Meet time bounds") from None
        records = await meet_request("records", params, credential().access_token or "")
        # Google may reject or under-match offset-bearing time filters. The
        # bounded meeting-code query is authoritative; select the instance locally.
        if not records:
            records = await meet_request(
                "records",
                {"filter": f'space.meeting_code = "{code}"', "pageSize": 10},
                credential().access_token or "",
            )
        selected = _select_record(records, event_start, event_end)
        name = selected.get("name") if isinstance(selected, dict) else None
        if not isinstance(name, str) or not name:
            raise GoogleValidationError("Meet conference record was malformed")
        return {"kind": "meet", "id": name.removeprefix("conferenceRecords/")}
    if kind == "drive" and value:
        from archie_agent.exec.tools.drive import read_reference

        result = await read_reference(value, "metadata", policy_snapshot)
        file_info = result.get("file", {})
        if file_info.get("mime_type") != "application/vnd.google-apps.document":
            raise GoogleValidationError("Drive Meet artifact is not a Google Doc")
        return {"kind": "drive", "id": value, "file": result}
    raise GoogleValidationError("unsupported meeting reference")


def _state(value: Any) -> str:
    return {
        "FILE_GENERATED": "available",
        "STARTED": "not_ready",
        "ENDED": "not_ready",
        "STATE_UNSPECIFIED": "not_ready",
    }.get(
        value, value if value in {"available", "not_ready", "missing", "unavailable"} else "missing"
    )


def _artifact(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "state": "missing",
            "content": None,
            "url": None,
            "provenance": "google.meet",
            "message": "No meeting artifact was returned by Google Meet.",
        }
    content = raw.get("content")
    if isinstance(content, bytes):
        content = None
    if isinstance(content, str):
        content = content[:30000]
    else:
        content = None
    destination = raw.get("docsDestination")
    if not isinstance(destination, dict):
        destination = {}
    state = _state(raw.get("state"))
    result = {
        "state": state,
        "content": content,
        "url": safe_url(raw.get("url") or destination.get("exportUri")),
        "provenance": "google.meet",
    }
    if state == "not_ready":
        result["message"] = "Meeting notes are not ready yet."
    elif state == "missing":
        result["message"] = "Google Meet did not return this meeting artifact."
    elif state == "unavailable":
        result["message"] = "This Google Meet artifact is currently unavailable."
    return result


async def _notes_from_drive(raw: dict[str, Any]) -> dict[str, Any] | None:
    destination = raw.get("docsDestination")
    document_id = destination.get("document") if isinstance(destination, dict) else None
    if not isinstance(document_id, str) or not document_id:
        return None
    from archie_agent.exec.tools.drive import read_reference

    result = await read_reference(document_id, "read")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, str):
        return None
    artifact = _artifact(raw)
    artifact["content"] = content[:30000]
    return artifact


async def _read(kind: str, meeting_id: str) -> dict[str, Any]:
    resolved = await resolve_meeting(meeting_id)
    token = credential().access_token or ""
    if resolved["kind"] == "meet":
        record = f"conferenceRecords/{resolved['id']}"
        artifacts = await meet_request(kind, {"record": record}, token)
    elif resolved["kind"] == "drive" and kind == "notes":
        from archie_agent.exec.tools.drive import read_reference

        result = await read_reference(resolved["id"], "read")
        content = result.get("content")
        if isinstance(content, str):
            return {
                "state": "available",
                "content": content[:30000],
                "url": result["file"].get("web_url"),
                "provenance": "google.meet",
            }
        return {
            "state": "missing",
            "content": None,
            "url": result["file"].get("web_url"),
            "provenance": "google.meet",
        }
    else:
        raise GoogleValidationError("Drive references support notes only")
    if not artifacts:
        return {"state": "missing", "content": None, "url": None, "provenance": "google.meet"}
    artifact = artifacts[0]
    if kind == "notes" and isinstance(artifact, dict):
        drive_result = await _notes_from_drive(artifact)
        if drive_result is not None:
            return drive_result
    return _artifact(artifact)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="meet")
async def notes(meeting_id: str) -> dict[str, Any]:
    return await _read("notes", meeting_id)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="meet")
async def transcript(meeting_id: str) -> dict[str, Any]:
    return await _read("transcript", meeting_id)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="meet")
async def recording(meeting_id: str) -> dict[str, Any]:
    result = await _read("recording", meeting_id)
    result["content"] = None
    return result


__all__ = ["notes", "transcript", "recording"]
