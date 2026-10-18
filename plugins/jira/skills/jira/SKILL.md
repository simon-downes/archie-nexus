---
name: jira
description: >
  Use for Jira project discovery, issue search and reads, user lookup, issue
  creation and updates, transitions, comments, and workspace-file attachments
  through the exec jira namespace. Not for raw Jira HTTP, raw JQL, credentials,
  or unsupported Jira APIs.
---

# Jira

## Global rules

- Call Jira only through `exec`, using `jira.<function>`.
- Credentials are implicit. Never request, pass, print, or store credentials, tokens, raw Jira URLs, or provider payloads.
- Reads are enabled by default; writes are disabled by default. Read and write project scopes are independent. Missing or empty scope means unrestricted.
- Tool calls return JSON-serializable values or raise an error; they do not return error objects.
- Raw JQL and arbitrary Jira HTTP are unavailable.
- All collections and text are bounded. The exec result may be truncated at 16,000 characters.
- Obtain explicit user intent before externally visible writes, especially comments, transitions, and uploads.

## Shared input rules

- **Project key:** 1–10 ASCII characters; starts with a letter, followed by letters, digits, or `_`. Keys are case-insensitive and returned uppercase. Project IDs are not accepted by `get_project`.
- **Issue key:** `<PROJECT>-<positive integer>`, for example `PLAT-123`. Keys are case-insensitive and normalized uppercase.
- **Limit:** integer in the documented range; `bool` is not a valid integer limit.
- **Text:** limits below count characters. Empty text is invalid unless explicitly documented as a clear operation.
- **Dates:** non-empty Jira-compatible date/time strings. No local date parsing is performed.
- **Assignee:** account ID, username, email, or display name. A write proceeds only when it resolves to exactly one active, assignable, non-app user.

## Shared output schemas

### Issue summary

```json
{
  "key": "PLAT-123",
  "summary": "Example",
  "status": "Open",
  "assignee": "Alex Smith",
  "parent": {"key": "PLAT-100", "summary": "Parent"},
  "priority": "High",
  "type": "Task",
  "labels": ["release"],
  "created": "2025-01-01T12:00:00Z",
  "updated": "2025-01-02T12:00:00Z"
}
```

Missing scalar fields are `null`; `parent` is an object or `null`; `labels` is always a list; timestamps are normalized to GMT/UTC strings when valid.

### Issue detail

An issue summary plus:

```json
{
  "description": "Plain text or null",
  "reported_by": "Name or null",
  "comments": [
    {
      "id": "10001",
      "author": "Name or null",
      "body": "Plain text",
      "created": "2025-01-01T12:00:00Z"
    }
  ],
  "attachments": [
    {"filename": "report.pdf", "size": 12345, "content_url": "https://..."}
  ]
}
```

At most 50 comments are returned. Description and comment bodies are plain text, not ADF.

## Read functions

### `jira.list_projects(limit=50)`

Returns up to `limit` project objects:

```json
{"id": "10000", "key": "PLAT", "name": "Platform"}
```

`limit` must be `1..100`. Read scope is applied while provider pages are accumulated; hidden projects are not counted or reported.

### `jira.get_project(project)`

Returns:

```json
{
  "project": {"id": "10000", "key": "PLAT", "name": "Platform"},
  "issue_types": [{"id": "10001", "name": "Task"}],
  "statuses": [{"id": "1", "name": "Open"}]
}
```

The metadata, issue-type, and status requests are all required; malformed or partial provider data is an error.

### `jira.list_issues(project=None, status=None, assignee=None, label=None, created_before=None, created_after=None, updated_before=None, updated_after=None, parent=None, limit=50)`

Uses only the named structured filters; `None` means omitted. Empty filter strings are invalid. Each supplied filter must be a non-empty string of at most 200 characters. `limit` must be `1..100`.

Returns a list of issue summaries. A configured read scope is added to generated JQL and cannot be bypassed. With no filters, results are ordered by most recently updated.

### `jira.get_issue(issue_key)`

Returns an issue detail object, including up to 50 most-recent comments and attachment metadata. A blocked issue is a policy error before provider access; an in-scope provider 404 is not-found.

### `jira.search_users(query, limit=20)`

Returns up to `limit` candidate objects without selecting one:

```json
{"account_id": "acct-1", "display_name": "Alex Smith", "email": "alex@example.com"}
```

`query` must be 1–200 characters. `limit` must be `1..50`. Ambiguous candidates are returned; this function never silently selects an assignee. User search is read-gated but not project-scoped.

## Write functions

All writes require enabled write policy and an in-scope project. Internal assignee lookup, transition discovery, and verification reads are also write-scoped.

### `jira.create_issue(project, summary, type="Task", description=None, labels=None, status=None, assignee=None, parent=None, priority=None)`

- `summary`: required, non-empty, at most 8,000 characters.
- `type`: required, non-empty issue-type name.
- `description`: optional plain text, at most 8,000 characters; converted to ADF.
- `labels`: optional list of replacement labels.
- `assignee`: optional value that must resolve uniquely.
- `parent`: optional issue key.
- `priority`: optional exact priority name.
- `status`: optional non-empty exact status name. Matching is case-insensitive; partial matches are not accepted.

Without `status`, returns `{"key": "PLAT-123", "id": "10001"}`. With `status`, performs creation, transition discovery, transition, and verification, then returns issue detail.

An ambiguous/unavailable assignee prevents creation. If creation succeeds but a requested follow-up status or verification fails, the result is indeterminate and includes the created key in the error; do not create again without verification.

### `jira.update_issue(issue_key, summary=None, description=None, labels=None, status=None, assignee=None, parent=None, priority=None)`

At least one argument must be supplied. `None` leaves a field unchanged:

| Field | Empty value | Non-empty value |
|---|---|---|
| `summary` | invalid | replaces summary; 1–8,000 characters |
| `description` | clears description | replaces description; at most 8,000 characters |
| `labels` | `[]` clears all labels | replaces all labels |
| `status` | invalid | exact, case-insensitive target status |
| `assignee` | `""` clears assignee | resolves uniquely and replaces assignee |
| `parent` | `""` clears parent | replaces with an issue key |
| `priority` | `""` clears priority | replaces with an exact priority name |

Returns the resulting issue detail. All local validation, including status validation, occurs before any mutation. A failed post-mutation read is indeterminate; verify before retrying.

### `jira.transition_issue(issue_key, status)`

`status` must be non-empty. The tool discovers transitions and mutates only when exactly one case-insensitive exact match exists. Unavailable or ambiguous statuses are rejected before mutation. Returns the verified issue detail.

### `jira.add_comment(issue_key, body)`

`body` must be plain text of 1–8,000 characters. Returns:

```json
{
  "id": "10001",
  "author": "Alex Smith",
  "body": "Plain text",
  "created": "2025-01-01T12:00:00Z"
}
```

The comment is externally visible and is converted to ADF. A transport or response failure after submission is indeterminate; verify with `get_issue` before repeating.

### `jira.attach_file(issue_key, path)`

Uploads one file under `/workspace`. Relative paths are workspace-relative; absolute paths must still be under `/workspace`.

Allowed extensions: `.png`, `.jpg`, `.jpeg`, `.gif`, `.pdf`, `.txt`, `.csv`, `.json`, `.zip`. Maximum size is inclusive 10 MiB. Before calling, disclose the selected path and filename to the user.

Returns attachment metadata only:

```json
{"id": "10001", "filename": "report.pdf", "size": 12345, "content_url": "https://..."}
```

File contents are never returned. An uncertain upload cannot currently be conclusively verified by `get_issue`; do not repeat it automatically.

## Errors and retry policy

Errors are sanitized and appear in the exec error envelope as a type and message. They do not contain credentials or raw provider payloads.

- `JiraConfigurationError`: credentials are missing, incomplete, or invalid. Do not retry.
- `JiraPolicyError` / `JiraWritePolicyError`: reads/writes are disabled or the resource is out of scope. Do not bypass or retry.
- `JiraAuthenticationError`: Jira rejected authentication. Do not retry unchanged.
- `JiraAuthorizationError`: Jira permissions denied the operation. Do not retry unchanged.
- `JiraNotFoundError`: an in-scope resource does not exist. Check the key, then report it.
- `JiraRateLimitError`: Jira throttled the request. Wait before retrying; no automatic retry is performed.
- `JiraInputError` / `JiraSearchValidationError` / `JiraValidationError`: input is invalid locally or rejected by Jira. Correct the input; do not retry unchanged.
- `JiraResponseError`: Jira returned an unexpected or malformed response. Do not automatically repeat a mutation.
- `JiraTransportError`: the request failed at the network boundary. For mutations, treat the outcome as unknown unless the tool explicitly reports a definite pre-transmission failure.
- `JiraMutationIndeterminateError`: a mutation may have succeeded or its verification failed. Verify with `get_issue` before repeating. Never automatically retry.

## Examples

```python
async def main():
    issues = await jira.list_issues(project="PLAT", status="Open", label="release", limit=10)
    return {"issue": await jira.get_issue(issues[0]["key"])} if issues else {"issue": None}
```

```python
async def main():
    return await jira.update_issue(
        "PLAT-123",
        description="",              # clear description
        assignee="user@example.com", # replace assignee
        # None-valued fields are omitted and remain unchanged.
    )
```
