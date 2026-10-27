---
name: incident-io
description: >
  Use for read-only incident.io investigation through the exec incident_io namespace:
  listing and reading incidents, timelines, updates, participants, alerts, actions,
  follow-ups and post-mortems, and looking up teams, users, catalog entries, on-call
  schedules, escalation paths and reference data. Not for declaring or changing
  incidents, escalating, paging, status pages, credentials, or raw incident.io API calls.
---

# incident.io

## Global rules

- Call incident.io only through `exec`, using `await incident_io.<function>(...)`.
- The tools are **read-only**. No function declares, updates, escalates, pages, or writes anything. Response actions belong in the incident Slack channel; use the `slack` skill only with explicit user intent.
- Credentials are implicit. Never request, pass, print, or store an API key, and do not make raw HTTP calls to incident.io.
- Reads are enabled by default. Policy can disable them:

  ```yaml
  plugins:
    incident_io:
      policy:
        read:
          enabled: false
  ```

  A `read.scope` is not supported; setting one makes every call fail with `IncidentIoPolicyError`.
- Calls return JSON-serializable values or raise typed errors; they do not return error objects.
- Results are normalised. Nested lists are capped at 50 entries; summaries, descriptions and post-mortem content are returned whole. The exec result shown to you is capped at 32,000 characters, but data inside the script is not. If a result may exceed the cap, process it in the script and return only the part you need, or write it to a file under `/tmp` and read it in chunks with `read(path, offset, limit)`. Request small `limit`s.
- Results are a point-in-time snapshot of a live system. Say when data was read if timing matters.

## Choose the right function

| Need | Function |
|---|---|
| Find incidents (active, recent, by severity or type) | `incident_io.list_incidents` |
| One incident: summary, roles, custom fields, participants, alert IDs | `incident_io.get_incident` |
| What happened, in order | `incident_io.incident_timeline` |
| Status and severity changes with messages | `incident_io.incident_updates` |
| Alerts, and the incidents an alert is linked to | `incident_io.list_alerts`, `incident_io.get_alert` |
| In-incident actions | `incident_io.list_actions` |
| Post-incident follow-ups | `incident_io.list_follow_ups` |
| Post-mortems and their content | `incident_io.list_postmortems`, `incident_io.get_postmortem` |
| Teams and members | `incident_io.list_teams` |
| Find a person by email or Slack ID | `incident_io.list_users` |
| Services and other catalog entries | `incident_io.list_catalog_types`, `incident_io.list_catalog_entries` |
| On-call schedules and who is on call | `incident_io.list_schedules`, `incident_io.on_call` |
| Escalation paths and current responders | `incident_io.list_escalation_paths` |
| Valid severities, types, statuses, roles, custom fields | `incident_io.reference` |

For an investigation, start with `get_incident`, then `incident_timeline` and `incident_updates`, then follow `alert_ids` with `get_alert`. For "have we seen this before?", use `list_incidents` with a date range and the same severity or type, then `list_follow_ups(incident=...)` to check whether earlier follow-ups were completed.

When a name matches nothing or several things, the error lists the valid names. Do not guess; pick from the list or ask the user.

## Shared input rules

- **Incident:** an incident ID (`01HX...`), a reference `INC-123` (case-insensitive), or the number `123`. All three forms work wherever `incident` is accepted.
- **Resource IDs** (`alert_id`, `postmortem_id`, `team`, alert `source`, `slack_user_id`): 1–100 letters, digits, `_` or `-`.
- **Names or IDs** (`severity`, `incident_type`, `status` on incidents, `schedule`, `catalog_type`): a non-empty string of at most 200 characters. Names match case-insensitively and exactly; partial matches are rejected. If a name matches several items, the error lists them with their IDs; pass the ID.
- **Lists:** filters that accept a list also accept a single string. Lists hold 1–20 values and match any of them.
- **Limit:** an integer `1..100` (`bool` is invalid). Lists are paginated internally until `limit` results or the end of the data.
- **Dates:**
  - `list_incidents` date filters take `YYYY-MM-DD` only. They are inclusive, and datetimes are rejected.
  - Other date filters take an ISO-8601 date or datetime; naive values are UTC. A bare date as a `..._before` bound covers that whole day.
  - Output timestamps are UTC strings such as `2026-10-01T10:00:00Z`.
- Invalid input raises `IncidentIoInputError`. Format errors are caught before any request; name lookups need one read of the reference list first.

## Output shapes

### User

```json
{"id": "01U...", "name": "Sam Smith", "email": "sam@example.com", "slack_user_id": "U123"}
```

Users appear inside other records (assignees, participants, responders). Fields may be `null`.

### Actor

A creator or updater is a user object, or `{"type": "api_key" | "workflow" | "alert", "name": "..."}` for automation, or `null`.

### Incident summary

```json
{
  "id": "01INC...",
  "reference": "INC-42",
  "name": "Checkout errors",
  "status": {"name": "Investigating", "category": "live"},
  "severity": "Major",
  "type": "Default",
  "mode": "standard",
  "visibility": "public",
  "created_at": "2026-10-01T10:00:00Z",
  "updated_at": "2026-10-01T11:00:00Z",
  "last_activity_at": "2026-10-01T11:00:00Z",
  "slack_channel_name": "inc-42-checkout",
  "permalink": "https://app.incident.io/..."
}
```

### Incident detail

An incident summary plus:

```json
{
  "summary": "Plain text or null",
  "creator": {"id": "...", "name": "...", "email": "...", "slack_user_id": "..."},
  "role_assignments": [{"role": "Incident Lead", "assignee": {"id": "...", "name": "..."}}],
  "custom_fields": [{"name": "Affected services", "values": ["checkout"]}],
  "timestamps": [{"name": "Reported at", "value": "2026-10-01T10:00:00Z"}],
  "durations": [{"name": "Time to fix", "seconds": 600}],
  "slack_channel_url": "https://...",
  "call_url": "https://... or null",
  "postmortem_document_ids": ["01PM..."],
  "participants": {"active": [{"id": "...", "name": "..."}], "passive": []},
  "alert_ids": ["01AL..."]
}
```

## Functions

### `incident_io.list_incidents(status_category=None, severity=None, incident_type=None, status=None, created_after=None, created_before=None, updated_after=None, updated_before=None, mode=None, sort="newest", limit=25)`

Returns incident summaries. `None` leaves a filter off; filters combine with AND.

- `status_category`: one or more of `triage`, `declined`, `merged`, `canceled`, `live`, `learning`, `closed`, `paused`. Active incidents are `live`.
- `severity`, `incident_type`, `status`: one or more names or IDs from `reference(...)`.
- `created_after`/`created_before`, `updated_after`/`updated_before`: inclusive `YYYY-MM-DD` dates.
- `mode`: one or more of `standard`, `retrospective`, `test`, `tutorial`. When omitted, test and tutorial incidents are excluded.
- `sort`: `newest` or `oldest` by creation time.

### `incident_io.get_incident(incident)`

Returns the incident detail, including participants and the IDs of linked alerts (up to 50).

### `incident_io.incident_timeline(incident, limit=25)`

Returns timeline items in incident.io's order:

```json
{"id": "...", "timestamp": "2026-10-01T10:00:00Z", "title": "...", "description": "... or null", "creator": {"type": "workflow", "name": "..."}}
```

### `incident_io.incident_updates(incident, limit=25)`

Returns status and severity updates:

```json
{"id": "...", "created_at": "...", "updater": {"id": "...", "name": "..."}, "message": "Fix deployed", "new_status": {"name": "Monitoring", "category": "live"}, "new_severity": "Minor"}
```

### `incident_io.list_alerts(status=None, source=None, created_after=None, created_before=None, limit=25)`

Returns alerts in incident.io's order, which is not guaranteed to be by time:

```json
{"id": "...", "title": "...", "status": "firing", "alert_source_id": "...", "deduplication_key": "...", "source_url": "...", "created_at": "...", "updated_at": "...", "resolved_at": null, "tags": ["checkout"]}
```

`status` is `firing` and/or `resolved`. `source` is one or more alert source IDs; take them from an alert's `alert_source_id`.

### `incident_io.get_alert(alert_id)`

Returns an alert plus `description`, `attributes` (`[{"name": "service", "values": ["checkout"]}]`), and `incident_ids` (the incidents it is linked to, up to 50).

### `incident_io.list_actions(incident=None, status=None, created_after=None, updated_after=None, limit=25)`

Returns actions from an incident's response:

```json
{"id": "...", "incident_id": "...", "description": "...", "status": "outstanding", "assignee": {...}, "creator": {...}, "created_at": "...", "updated_at": "...", "completed_at": null}
```

### `incident_io.list_follow_ups(incident=None, status=None, team=None, created_after=None, updated_after=None, limit=25)`

Returns post-incident follow-ups:

```json
{"id": "...", "incident_id": "...", "title": "...", "description": "...", "status": "outstanding", "priority": "High", "assignee": {...}, "assignee_team": "Payments", "labels": [], "external_issue": {"provider": "jira", "name": "PLAT-1", "url": "https://..."}, "created_at": "...", "updated_at": "...", "completed_at": null}
```

For both actions and follow-ups:

- `status` is one or more of `outstanding`, `completed`, `deleted`, `not_doing`.
- The status filter is applied after fetching, over at most 20 pages. A rare status may return fewer than `limit` results. Narrow with `incident` or a date filter to be sure.
- `team` (follow-ups only) is a team ID from `list_teams`.

### `incident_io.list_postmortems(incident=None, limit=25)`

Returns post-mortem documents, newest first:

```json
{"id": "...", "incident_id": "...", "title": "...", "status": "completed", "type": "in_app", "document_url": "https://...", "editors": [{...}], "created_at": "...", "updated_at": "..."}
```

### `incident_io.get_postmortem(postmortem_id)`

Returns a post-mortem plus `content`, its full Markdown. `content` is `null` when the document has no in-app content.

### `incident_io.list_teams(limit=25)`

Returns `{"id", "name", "members": [user, ...]}` per team.

### `incident_io.list_users(email=None, slack_user_id=None, include_inactive=False, limit=25)`

Returns users plus `is_active` and `base_role`. `email` and `slack_user_id` are exact matches. Use them to map a Slack user to an incident.io user.

### `incident_io.list_catalog_types()`

Returns catalog types:

```json
{"id": "...", "name": "Service", "type_name": "Custom[\"Service\"]", "description": "...", "is_team_type": false, "estimated_count": 12, "attributes": [{"id": "...", "name": "Owner", "type": "..."}]}
```

### `incident_io.list_catalog_entries(catalog_type, identifier=None, limit=25)`

`catalog_type` is a type ID, `type_name`, or display name such as `Service`. `identifier` finds entries by ID, external ID, alias, or name. Returns:

```json
{"id": "...", "name": "checkout", "external_id": "...", "aliases": ["co"], "attributes": {"Owner": ["Payments"]}, "archived_at": null}
```

Attribute keys are attribute names when the type's schema names them.

### `incident_io.list_schedules(limit=25)`

Returns schedules with `timezone`, `team_ids`, `rotations` (`[{"id", "name"}]`), `current_shifts`, `next_shifts`, and `permalink`. A shift is `{"user", "start_at", "end_at", "rotation_id"}`.

### `incident_io.on_call(schedule, start=None, end=None)`

Returns who is on call for one schedule, with overrides applied:

```json
{"schedule_id": "...", "start": "2026-10-01T10:00:00Z", "end": "2026-10-01T10:01:00Z", "entries": [{"user": {...}, "start_at": "...", "end_at": "...", "rotation_id": "..."}], "entries_truncated": false}
```

- `schedule`: a schedule ID or exact name.
- Omit both `start` and `end` to see who is on call now. The window is then now plus one minute.
- With only `start`, the window is one minute long.
- `end` must be after `start`, and the window can be at most 31 days.
- At most 50 entries are returned. When `entries_truncated` is true, the roster is incomplete; query a shorter window.

### `incident_io.list_escalation_paths(limit=25)`

Returns `{"id", "name", "team_ids", "current_responders": [user, ...], "levels"}`. `levels` is the number of top-level steps in the path. The tool reads escalation paths but never triggers them.

### `incident_io.reference(kind)`

`kind` is one of `severities`, `incident_types`, `incident_statuses`, `incident_roles`, `custom_fields`. Returns `{"id", "name", "description"}` plus `rank`, `category`, `field_type`, `shortform`, `role_type` or `is_default` where the kind has them. Use it to find valid filter names.

## Errors and retry policy

Errors are sanitized. They never contain credentials or raw provider payloads.

- `IncidentIoConfigurationError`: credentials are missing or incomplete. Do not retry; tell the user to run `archie auth login incident_io`.
- `IncidentIoPolicyError`: reads are disabled or an unsupported scope is set. Do not bypass or retry.
- `IncidentIoInputError`: local validation failed, or a name matched nothing or several items (the message lists valid names or the matching IDs). Only reference lookups were made. Correct the input.
- `IncidentIoRequestRejectedError`: incident.io rejected the request (HTTP 400/422). The message may include provider error codes. Correct the input; do not retry unchanged.
- `IncidentIoAuthenticationError`: the API key was rejected. Do not retry.
- `IncidentIoAuthorizationError`: the key lacks permission for this data. Do not retry.
- `IncidentIoNotFoundError`: the ID or reference does not exist. Check it, then report it.
- `IncidentIoRateLimitError`: throttled. `retry_after_seconds` is an integer or `None`. Wait at least that long before one retry; nothing retries automatically.
- `IncidentIoTransportError`: the network failed or the 60-second call limit expired. Reads are safe to retry once.
- `IncidentIoResponseError`: incident.io returned something unexpected. Report it; retry at most once.

## Examples

```python
async def main():
    live = await incident_io.list_incidents(status_category="live", limit=5)
    if not live:
        return "No live incidents"
    incident = await incident_io.get_incident(live[0]["reference"])
    timeline = await incident_io.incident_timeline(incident["id"], limit=20)
    return {
        "incident": {k: incident[k] for k in ("reference", "name", "severity", "status")},
        "lead": [r for r in incident["role_assignments"] if r["role"] == "Incident Lead"],
        "timeline": [(t["timestamp"], t["title"]) for t in timeline],
    }
```

```python
async def main():
    schedules = await incident_io.list_schedules(limit=10)
    return {
        s["name"]: [e["user"]["name"] for e in (await incident_io.on_call(s["id"]))["entries"]]
        for s in schedules
    }
```
