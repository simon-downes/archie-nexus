"""incident.io on-call schedules and escalation paths (read-only)."""

from __future__ import annotations

from archie_agent.exec.tools import tool

from . import validation as v
from .client import (
    IncidentIoClient,
    IncidentIoResponseError,
    incident_io_client,
)
from .formatting import MAX_NESTED, escalation_path, shift
from .formatting import schedule as format_schedule
from .policy import require_read

_ESCALATION_PAGE = 25  # /v2/escalation_paths rejects page_size above 25


@tool(native=False, exec_docs=False, namespace="incident_io")
async def list_schedules(limit: int = 25) -> list[dict]:
    """List on-call schedules with rotations and current and next shifts."""
    count = v.limit(limit)
    require_read()
    async with incident_io_client() as client:
        items = await client.paginate("/v2/schedules", "schedules", count)
    return [format_schedule(item) for item in items]


async def _schedule_id(client: IncidentIoClient, value: str) -> str:
    """Resolve a schedule ID or case-insensitive exact name."""
    items = await client.paginate("/v2/schedules", "schedules", 100)
    return str(v.choose(items, value, "schedule")["id"])


@tool(native=False, exec_docs=False, namespace="incident_io")
async def on_call(schedule: str, start: str | None = None, end: str | None = None) -> dict:
    """Show who is on call for a schedule in a window (default: now), overrides applied."""
    wanted = v.text(schedule, "schedule")
    begin, finish = v.window(start, end)
    require_read()
    async with incident_io_client() as client:
        schedule_id = await _schedule_id(client, wanted)
        data = await client.get(
            "/v2/schedule_entries",
            {
                "schedule_id": schedule_id,
                "entry_window_start": v.iso(begin),
                "entry_window_end": v.iso(finish),
            },
        )
    entries = data.get("schedule_entries")
    final = entries.get("final") if isinstance(entries, dict) else None
    if not isinstance(final, list):
        raise IncidentIoResponseError("incident.io returned malformed schedule entries")
    return {
        "schedule_id": schedule_id,
        "start": v.iso(begin),
        "end": v.iso(finish),
        "entries": [shift(item) for item in final[:MAX_NESTED] if isinstance(item, dict)],
        "entries_truncated": len(final) > MAX_NESTED,
    }


@tool(native=False, exec_docs=False, namespace="incident_io")
async def list_escalation_paths(limit: int = 25) -> list[dict]:
    """List escalation paths with their teams and current responders."""
    count = v.limit(limit)
    require_read()
    async with incident_io_client() as client:
        items = await client.paginate(
            "/v2/escalation_paths", "escalation_paths", count, max_page_size=_ESCALATION_PAGE
        )
    return [escalation_path(item) for item in items]
