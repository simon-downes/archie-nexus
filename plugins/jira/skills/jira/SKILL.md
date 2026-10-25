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

- Use Jira only through the `jira` namespace, calling `jira.<function>` from `exec`.
- Credentials are implicit, so never provide credentials. Never request, pass, print, or store credentials, tokens, raw Jira URLs, or provider payloads.
- Reads are enabled by default; writes are disabled by default. Read and write project scopes are independent. Missing or empty scope means unrestricted.
- Tool calls return JSON-serializable values or raise an error; they do not return error objects.
- Use structured Jira filters (`jira.list_issues` arguments); raw JQL and arbitrary Jira HTTP are not available.
- Fetch issue details with `jira.get_issue` before requesting comments; the detail already includes the most recent comments.
- Collections are bounded; issue descriptions and comment bodies are returned whole. The exec result shown to you is capped at 32,000 characters, but data inside the script is not. If a result may exceed the cap, process it in the script and return only the part you need, or write it to a file under `/tmp` and read it in chunks with `read(path, offset, limit)`.
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
  "description": "Markdown or null",
  "reported_by": "Name or null",
  "comments": [
    {
      "id": "10001",
      "author": "Name or null",
      "body": "Markdown",
      "created": "2025-01-01T12:00:00Z"
    }
  ],
  "attachments": [
    {"filename": "report.pdf", "size": 12345, "content_url": "https://..."}
  ]
}
```

At most 50 comments are returned. Description and comment bodies are Markdown in the same dialect writes accept (see "Formatting descriptions and comments"), so text you read can be edited and saved back with its formatting intact.

Jira content with no Markdown form is shown as readable text: an expand section as its bold title followed by its contents, a mention as `@Name`, a status as `[STATUS]`, a date as `YYYY-MM-DD`, an attachment as `[attachment: name]`, a smart link as `<url>`, and task items as `- [ ]` / `- [x]`. Underline and colour are dropped. **Re-saving such a description turns these into plain text**: mentions stop notifying, expands open, task boxes stop being checkable, and embedded attachments become text. Before re-saving a description that contains any of them, tell the user what will change, or edit with a comment instead.

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

### Formatting descriptions and comments

Descriptions and comment bodies are Markdown (CommonMark with GFM tables and strikethrough), converted to Jira's rich text. Write readable structure: short sections with headings, lists, and tables rather than long paragraphs.

- Supported: headings `#`–`######`, paragraphs, line breaks (a single newline is kept as a line break), `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `[links](https://…)`, bullet and numbered lists (nested; a numbered list may start at any number), fenced code blocks with a language, `>` quotes, `---` rules, and tables with a header row.
- Panels: a quote whose first line is exactly `> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, `> [!WARNING]`, or `> [!CAUTION]` becomes an info, success, note, warning, or error panel.
- Not supported: raw HTML (sent as literal text), @mentions (sent as plain text; no one is notified), images (sent as a link), task checkboxes, expand sections, status lozenges, colours.
- Jira cannot nest some blocks. A heading inside a list or panel becomes bold text, a table inside a list becomes one line per row, a quote inside a quote is flattened, and a code block inside a panel becomes code-formatted lines. Content is kept.

### `jira.create_issue(project, summary, type="Task", description=None, labels=None, status=None, assignee=None, parent=None, priority=None)`

- `summary`: required, non-empty, at most 8,000 characters.
- `type`: required, non-empty issue-type name.
- `description`: optional Markdown, at most 32,767 characters (Jira's limit); converted to ADF. Blank or whitespace-only text is omitted.
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
| `description` | clears description (also whitespace-only) | replaces description; Markdown, at most 32,767 characters |
| `labels` | `[]` clears all labels | replaces all labels |
| `status` | invalid | exact, case-insensitive target status |
| `assignee` | `""` clears assignee | resolves uniquely and replaces assignee |
| `parent` | `""` clears parent | replaces with an issue key |
| `priority` | `""` clears priority | replaces with an exact priority name |

Returns the resulting issue detail. All local validation, including status validation, occurs before any mutation. A failed post-mutation read is indeterminate; verify before retrying.

### `jira.transition_issue(issue_key, status)`

`status` must be non-empty. The tool discovers transitions and mutates only when exactly one case-insensitive exact match exists. Unavailable or ambiguous statuses are rejected before mutation. Returns the verified issue detail.

### `jira.add_comment(issue_key, body)`

`body` must be Markdown of 1–32,767 characters and not blank. Returns:

```json
{
  "id": "10001",
  "author": "Alex Smith",
  "body": "Markdown",
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
