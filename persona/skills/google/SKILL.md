---
name: google
description: >
  Use for bounded Google Workspace discovery, Gmail reads and label/archive
  operations, Calendar reads, Drive metadata/content reads, and Meet artifact
  references through the exec Google namespaces. Not for credentials, raw Google
  API calls, arbitrary HTTP, unsupported mutations, recording downloads, or
  participant exports.
---

# Google Workspace

## Global rules

- Call Google only through `exec`, using the exact namespace-qualified function names documented below.
- There are no aliases. Do not invent names such as `calendar.today`; use the exact public names.
- Credentials are implicit. Never request, pass, print, store, or expose OAuth tokens, credentials, raw provider payloads, or arbitrary API responses.
- Google tool policy is controlled only by these flags:

  ```yaml
  tools:
    google:
      read:
        enabled: true
      write:
        enabled: false
  ```

- Reads default to enabled when omitted. Writes default to disabled when omitted. Policy is snapshotted at session start; changing `config.yaml` requires a new session.
- Google Nexus policy has no resource scopes, service blocks, operation allowlists, or OAuth-scope requirements. OAuth scopes are provider configuration and must be changed through the Google authentication setup.
- All public tools return bounded JSON-serializable projections or raise typed errors. They do not return error objects.
- Validate arguments before credentials or provider calls. Do not bypass a policy denial by using another Google API, raw HTTP, or a sibling public tool.
- The exec result has a global 16,000-character limit in addition to the service-level bounds below.
- Obtain explicit user confirmation before Gmail label changes or archiving. Confirm the exact message and requested mutation.
- Never automatically repeat an uncertain Gmail mutation. For bulk mutations, the tool does not perform implicit post-error verification; any later user-directed read is a separate operation, not a retry.

## Exact public surface

| Namespace | Public functions |
|---|---|
| `google` | `search` |
| `mail` | `search`, `read`, `list_labels`, `label`, `archive`, `unarchive`, `mark_read`, `mark_unread`, `star`, `unstar` |
| `calendar` | `today`, `upcoming`, `event`, `search` |
| `drive` | `search`, `list`, `metadata`, `read` |
| `meet` | `notes`, `transcript`, `recording` |

All listed functions are exec-only. No Google function is a native tool, and no other Google function should be assumed to exist.

## Shared input rules

- **Text:** strings are trimmed, must be non-empty where documented, must not contain control characters, and are bounded by the function-specific limit.
- **IDs:** resource IDs are non-empty, control-free strings. Gmail, Calendar, and Drive IDs are at most 256 characters. Meet references are at most 500 characters and must use a documented canonical prefix.
- **Limits:** limits must be real integers; `bool` is invalid. A limit outside the documented range is rejected before provider access.
- **URLs:** returned URLs are references only. Never follow arbitrary URLs, and never treat a provider URL as permission to access another resource.
- **Empty results:** an empty list is a successful result, not evidence that a provider or credential is unavailable.

## Shared output rules

Outputs contain only approved, bounded fields. Provider-specific fields not shown below must not be assumed to be available.

### Discovery reference

```json
{
  "type": "email",
  "id": "message-id",
  "title": "Message title",
  "summary": "Bounded summary or null",
  "source": "gmail",
  "timestamp": "2025-01-01T12:00:00Z",
  "url": "https://...",
  "provenance": "google.gmail",
  "handoff": "mail.read"
}
```

Fields may be `null` where the provider has no safe value. Discovery never returns bodies, document content, transcripts, notes, recording bytes, participant exports, tokens, or raw payloads.

### Gmail message

```json
{
  "id": "message-id",
  "thread_id": "thread-id",
  "subject": "Subject or null",
  "from": "Sender or null",
  "to": "Recipient or null",
  "date": "2025-01-01T12:00:00Z",
  "snippet": "Bounded snippet or null",
  "body": "Bounded body or null",
  "label_ids": ["INBOX", "UNREAD"]
}
```

Do not assume attachments, complete headers, arbitrary MIME parts, or full unbounded message content are available.

### Calendar event

```json
{
  "id": "event-id",
  "summary": "Meeting title or null",
  "start": "2025-01-01T12:00:00Z",
  "end": "2025-01-01T13:00:00Z",
  "status": "confirmed",
  "organizer": "person@example.com",
  "meet_url": "https://meet.google.com/...",
  "provenance": "google.calendar"
}
```

Meet URLs are nullable safe references. Do not infer a Meet URL from arbitrary event text.

### Drive result

Drive metadata and read results contain bounded normalized metadata. A read result may use this envelope:

```json
{
  "file": {
    "id": "file-id",
    "name": "Document",
    "mime_type": "application/vnd.google-apps.document",
    "modified_time": "2025-01-01T12:00:00Z",
    "web_url": "https://drive.google.com/...",
    "trashed": false,
    "provenance": "google.drive"
  },
  "folder_id": "folder-id",
  "format": "text",
  "content": "Bounded approved content or null",
  "truncated": false,
  "provenance": "google.drive"
}
```

Drive may return a reference-only result for binary, unknown, executable, HTML, or otherwise unsafe formats.

### Meet artifact

```json
{
  "state": "available",
  "content": "Bounded notes or transcript text, when available",
  "url": "https://drive.google.com/...",
  "provenance": "google.meet"
}
```

Successful Meet calls use exactly one state: `available`, `not_ready`, `missing`, or `unavailable`. These are result states, not errors. Recording results contain links and metadata only; they never contain recording bytes.

## Discovery

### `google.search(query, types=None, limit=20)`

Searches bounded metadata across Gmail, Calendar, Drive, and conditionally Meet.

- `query`: trimmed string of 1–500 characters.
- `types`: `None` searches `email`, `event`, and `file`; otherwise it must be a non-empty duplicate-free list containing only `email`, `event`, `file`, and `meeting`.
- `limit`: integer from 1–20.
- Results are deduplicated by canonical `(type, id)` and globally capped at `limit`.
- Source results are bounded; discovery does not expose provider cursors or raw search responses.
- An explicit `meeting` request may return a sanitized configuration or authorization error when Meet is unavailable. Do not retry unchanged or fall back to arbitrary Meet search.

Use handoffs exactly as returned:

| Reference type | ID field | Follow-up |
|---|---|---|
| `email` | message ID | `mail.read(message_id)` |
| `event` | event ID | `calendar.event(event_id)` |
| `file` | file ID | `drive.metadata(file_id)` |
| `meeting` | provider-specific canonical reference | Use the documented Meet reference only; do not guess IDs |

### Safe discovery flow

```python
async def main():
    results = await google.search("launch plan", types=["email", "file"], limit=10)
    return results
```

Inspect `type`, `id`, and `handoff` before calling a detail tool. Never pass a thread ID where a Gmail message ID is required.

## Gmail

All Gmail reads require `tools.google.read.enabled`. Gmail mutations require `tools.google.write.enabled`. Mutation functions accept 1–500 message IDs; callers must batch larger sets themselves. `mail.label` may require Google read access on a cache miss while resolving USER label names. Dedicated system mutations do not require Google read access.

### Read functions

#### `mail.search(query, limit=20)`

Searches Gmail messages.

- `query`: 1–500 characters.
- `limit`: integer from 1–100.
- Results are bounded message projections and do not include attachment bytes.
- Use the returned `id` as the message ID for `mail.read`; do not substitute `thread_id`.

#### `mail.read(message_id)`

Reads one message by a non-empty, control-free ID of at most 256 characters. The result is a bounded message projection.

#### `mail.list_labels()`

Returns normalized existing labels. Use it to inspect available labels before a label mutation. Label creation is unsupported.

### Write functions

#### `mail.label(message_ids, add=None, remove=None)`

Applies or removes USER labels by human-readable name on 1–500 messages in one Gmail batch mutation. Callers must batch larger sets. System labels are rejected; use dedicated operations below.

- `add` and `remove` contain human label names, not Gmail IDs.
- Label names must exist and must not overlap between add/remove.
- The result is an aggregate envelope such as `{"count": 3, "complete": true}`.

#### `mail.archive(message_ids)` / `mail.unarchive(message_ids)`

Archives or unarchives 1–500 messages by removing or adding `INBOX`.

#### `mail.mark_read(message_ids)` / `mail.mark_unread(message_ids)`

Marks 1–500 messages read or unread by removing or adding `UNREAD`.

#### `mail.star(message_ids)` / `mail.unstar(message_ids)`

Stars or unstars 1–500 messages by adding or removing `STARRED`.

All mutation operations make one Gmail `batchModify` request. They do not pre-read each message and must not be automatically retried after an uncertain response.

### Safe Gmail flow

```python
async def main():
    messages = await mail.search("from:alerts@example.com", limit=5)
    if not messages:
        return {"message": None}
    message = await mail.read(messages[0]["id"])
    return message
```

Before a mutation, identify the exact bounded message set and obtain user confirmation for the requested operation. The confirmation may present the selected message IDs or a user-visible message summary/count whose IDs were deterministically obtained:

```python
# Only call after confirming the exact selected messages and mutation.
result = await mail.mark_read(["message-id-1", "message-id-2"])
```

## Calendar

Calendar is read-only. There are no public Calendar write functions.

### `calendar.today()`

Returns events intersecting the current calendar day in the resolved timezone. Today is a calendar-day window, not an arbitrary rolling 24-hour period. All-day events intersecting the window are included. Recurring events are expanded into instances, and declined/hidden invitations are retained so the result reflects the calendar data rather than only accepted events.

### `calendar.search(start, end, query=None, limit=50)`

Searches event titles and descriptions within an explicit timezone-aware ISO-8601 window. Use this for historical date questions and date-specific searches; do not use `google.search` for Calendar date retrieval.

- `start` and `end`: timezone-aware ISO-8601 timestamps, with `start < end`.
- `query`: optional; when provided, a 1–500 character title/description search string. Omit it to return all events in the date window.
- `limit`: integer from 1–50.
- Recurring instances are expanded. Declined and hidden invitations are included when Google returns them.

### `calendar.upcoming(days=7, limit=50)`

Returns events intersecting the next `days` calendar days.

- `days`: non-boolean integer from 1–31.
- `limit`: integer from 1–50.
- Event windows are timezone-aware and deterministic for the invocation.

### `calendar.event(event_id)`

Reads one event by a non-empty, control-free ID of at most 256 characters. Use an event ID from Discovery or another documented handoff. Do not invent a Calendar search function or pass arbitrary URLs.

## Drive

Drive is read-only. No Drive mutations, binary downloads, arbitrary URL fetches, or raw provider payloads are available. Drive operations require the Google read flag.

### `drive.search(query, limit=20)`

Searches bounded Drive metadata.

- `query`: 1–500 characters.
- `limit`: integer from 1–50.

### `drive.list(limit=100)`

Lists bounded Drive metadata.

- `limit`: integer from 1–100.
- Trashed and malformed records are not assumed to be usable; inspect normalized fields.

### `drive.metadata(file_id)`

Reads bounded metadata for one file ID. Discovery hands off file IDs to this function.

### `drive.read(file_id, format="auto")`

Reads approved textual content only after a metadata pre-read.

- `file_id`: non-empty, control-free string of at most 256 characters.
- `format`: `auto`, `text`, `csv`, or `reference`.
- `auto` resolves Google Docs/Slides to text, Sheets to CSV, and approved textual MIME types to text.
- Approved textual MIME types are `text/plain`, `text/markdown`, `text/csv`, `application/json`, and `application/xml`.
- Binary, unknown, executable, HTML, and unsafe formats remain reference-only.
- Content is bounded, normalized, and may be truncated. Never expect arbitrary file bytes.
- Drive requests have a 30-second deadline.

## Meet

Meet has no free-text search, mutation, recording download, participant export, or arbitrary artifact download. All Meet tools require the Google read flag.

### Canonical meeting references

Meeting IDs must use one of these forms:

- `calendar:<event_id>` — preferred handoff from `calendar.event`, `calendar.today`, `calendar.upcoming`, or `calendar.search`. The Meet tool fetches the normalized Calendar event privately, extracts its safe Meet URL/code, resolves exactly one matching conference record, and then fetches the artifact.
- `meet:<conference_record_id>` — direct conference-record reference when one is already known.
- `drive:<file_id>` — reserved for correlated Meet artifacts; arbitrary Drive files are not valid meeting references.

When Calendar returns an event with `id` and `meet_url`, pass the event ID as `calendar:<id>`; do not pass the entire event object, Meet URL, or a guessed conference-record ID. Resolution rejects malformed, missing, or ambiguous candidates. It is bounded to at most 10 candidates, 20 seconds total, and 8 seconds per provider request. Meet uses private Calendar and Drive seams; do not resolve a sibling by looking up its public exec tool.

### Artifact functions

#### `meet.notes(meeting_id)`

Returns bounded notes when available. Smart Notes are preferred. Drive fallback notes require Google Doc MIME, meeting correlation, and source provenance; a generic title match is insufficient.

#### `meet.transcript(meeting_id)`

Returns a bounded transcript or one of the documented availability states. Text is capped at 30,000 characters.

#### `meet.recording(meeting_id)`

Returns recording metadata and safe links only. It never downloads or returns recording bytes.

Treat `not_ready`, `missing`, and `unavailable` as valid result states. Do not retry indefinitely or reinterpret them as authorization failures.

## Errors and retry policy

Errors are sanitized and do not contain credentials, tokens, hidden resources, raw message/document content, or raw provider payloads.

- `GoogleValidationError`: an input is invalid. Correct it; do not retry unchanged.
- `GoogleConfigurationError`: Google credentials or provider configuration are missing/incomplete. Authenticate or configure Google through Nexus; do not pass credentials to a tool.
- `GoogleAuthenticationError`: Google rejected or expired authentication. Reauthenticate or refresh through Nexus; do not retry unchanged.
- `GoogleAuthorizationError`: Google or Nexus policy denied the operation. A provider 403 is authoritative; do not bypass policy or retry unchanged.
- `GoogleNotFoundError`: the requested in-scope resource was not found. Verify the canonical ID before reporting or retrying.
- `GoogleRateLimitError`: Google throttled the request. Wait before a deliberate retry of the same bounded read; never broaden the query automatically.
- `GoogleTransportError`: the network boundary, timeout, or response format failed. A read may be retried deliberately after checking connectivity. Treat a mutation outcome as unknown unless failure is known to have occurred before transmission.
- Meet ambiguity, invalid correlation, and unsupported artifact states must not be silently resolved by choosing the first candidate.
- Never automatically repeat Gmail label changes or archive operations after an uncertain response. Re-read the message and verify labels first.

## Safe cross-service examples

### Discovery to detail

```python
async def main():
    results = await google.search("roadmap", types=["email", "file"], limit=10)
    if not results:
        return {"results": []}
    item = results[0]
    if item["type"] == "email":
        return await mail.read(item["id"])
    if item["type"] == "file":
        return await drive.metadata(item["id"])
    return item
```

### Calendar event to Meet artifact

```python
async def main():
    events = await calendar.upcoming(days=7, limit=10)
    if not events:
        return {"meeting": None}
    # Use the canonical event ID; ask the Meet tool to resolve the event.
    return await meet.notes(f"calendar:{events[0]['id']}")
```

### Confirm before Gmail archive

```python
async def main():
    messages = await mail.search("label:todo", limit=10)
    # Before the next call, confirm the exact selected message and archive action.
    return messages
```

## Unsupported capabilities

The following are intentionally unavailable:

- Raw Google HTTP, arbitrary Google API methods, arbitrary URLs, and credentials as arguments.
- Gmail send, reply, forward, delete, attachment-byte access, label creation, and thread-ID substitution for message IDs.
- Calendar search, event creation, updates, deletion, attendee mutation, and other writes.
- Drive mutations, binary downloads, executable/HTML retrieval, arbitrary content formats, and unrestricted file access.
- Free-text Meet search, participant export, recording download, recording bytes, Meet mutation, and generic title-only artifact matching.
- Public sibling-tool lookup for Calendar/Drive/Meet correlation.
- Nexus resource scopes, Google service blocks, operation allowlists, and local verification of granted OAuth scopes.