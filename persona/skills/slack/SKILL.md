---
name: slack
description: >
  Use for bounded Slack user and conversation discovery, message search/history,
  thread reads, and explicitly authorized message/reaction writes through the exec
  slack namespace. Not for credentials, raw Slack API calls, administration, or
  unsupported operations.
---

# Slack

## Global rules

- Call Slack only through `exec`, using `slack.<function>`.
- Credentials are implicit. Never request, pass, print, store, or expose tokens or raw provider data.
- Reads and writes are independently policy-gated. Reads are enabled by default; writes are disabled by default unless explicitly enabled.
- The configured Slack policy uses the shared tool envelope:

  ```yaml
  tools:
    slack:
      read:
        enabled: true
        scope:
          deny: []
      write:
        enabled: false
        scope:
          deny: []
  ```

- `scope.deny` entries are independent for reads and writes. They may identify a canonical `C...`, `G...`, or `D...` conversation ID, or a channel as `#name`. Denied targets and identities must not be disclosed.
- Tool calls return JSON-serializable values or raise typed errors; they do not return error objects.
- Obtain explicit user intent before `send_message` or `react`. Confirm the target, text/reaction, action, and thread when ambiguity exists.
- Conversation references currently resolve reliably as canonical Slack IDs. Discover IDs with `slack.conversations` before direct reads or writes; do not invent provider search syntax or pass unsupported parameters.
- The exec result has a global 16,000-character limit in addition to service-level bounds below.

## Choose the right function

| Need | Function |
|---|---|
| Find people | `slack.users` |
| Find permitted conversations and IDs | `slack.conversations` |
| Search Slack content | `slack.search` |
| Read conversation history | `slack.messages` |
| Read a thread | `slack.thread` |
| Send a message or reply | `slack.send_message` |
| Read reactions | `slack.reactions` |
| Add or remove a reaction | `slack.react` |

When multiple people or conversations match, do not choose silently; ask the user to clarify. Use returned conversation IDs exactly as provided. Never infer a DM ID from a user ID.

## Shared input rules

- **Conversation ID:** a non-empty string, normally `C...` for channels, `G...` for private channels/group DMs, or `D...` for one-to-one DMs. Direct target tools use the canonical resolved ID, not an arbitrary `user_id` argument.
- **Limit:** a real integer; `bool` is invalid. Search accepts `1..20`; messages and threads accept `1..100`; directory results are capped at 500.
- **Search query:** non-empty string, at most 1,000 characters after trimming.
- **Search arrays:** `content_types` values are `messages`, `files`, `channels`, or `users`; `channel_types` values are `public_channel`, `private_channel`, `im`, or `mpim`. Supplied lists must be non-empty and duplicate-free. Defaults are messages and all four channel types.
- **Search bounds:** `after` and `before` are timezone-aware ISO-8601 values. They are exclusive, and `after` must be earlier than `before`.
- **Slack timestamps:** messages, threads, sends, and reactions use positive Slack timestamps such as `1712345678.000001`; timestamp strings must have six fractional digits where documented below.
- **Text:** limits count Unicode characters. Text is not silently removed or rewritten, except that `send_message` prepends `:archie: ` to its outgoing fallback text.

## Output projections

### User

```json
{
  "id": "U123",
  "username": "henry.dennis",
  "real_name": "Henry Dennis",
  "display_name": "Henry",
  "email": "henry@example.com",
  "deleted": false,
  "is_bot": false
}
```

Bots are never returned. Deleted users are excluded unless `include_deleted=True`. Profile fields and raw provider data are not returned.

### Conversation

```json
{
  "id": "D123",
  "type": "dm",
  "name": null,
  "is_private": null,
  "is_muted": false,
  "is_archived": false,
  "participants": [{"id": "U123"}]
}
```

Archived conversations are excluded. Channel participants are an empty list. Results contain no topics, purposes, permissions, member counts, or message content.

### Message

Search, history, and thread results use bounded projections such as:

```json
{
  "id": "1712345678.000001",
  "type": "message",
  "timestamp": "2025-01-01T00:00:00.000001Z",
  "author": {
    "id": "U123",
    "username": null,
    "real_name": null,
    "display_name": null,
    "is_deleted": null,
    "is_bot": null
  },
  "text": "Message text",
  "thread_timestamp": null,
  "reply_count": null,
  "permalink": "https://workspace.slack.com/..."
}
```

Raw blocks, attachments, reactions, profiles, and provider-specific fields are excluded.

## Read functions

### `slack.users(query=None, *, refresh=False, include_deleted=False)`

Finds people in the permitted normalized user directory. `query` is optional and capped at 200 characters. Matching is case-insensitive across IDs, usernames, real names, display names, and available email. Use `refresh=True` when directory data may be stale. Results exclude bots and are capped at 500.

### `slack.conversations(*, types=None, query=None, refresh=False)`

Finds permitted conversations in which the authenticated user participates and returns canonical IDs. `types` is `None` for all types or a distinct non-empty list containing only `channel`, `dm`, and `group_dm`; values such as `im` and `mpim` are not public tool values. `query` is a local case-insensitive filter capped at 200 characters. Archived conversations are excluded and muted conversations are retained. Use `refresh=True` when conversation data may be stale. Results are capped at 500.

Use this function to discover a DM ID before reading it:

```python
async def main():
    people = await slack.users(query="Henry Dennis")
    dms = await slack.conversations(types=["dm"])
    return {"people": people, "dms": dms}
```

### `slack.search(query, *, conversation=None, content_types=None, channel_types=None, after=None, before=None, sort="relevance", include_context=True, limit=20)`

Performs one bounded search. It does not automatically paginate or retry; make a new call when a different result set is needed. Search defaults to messages and all public/private/DM/group-DM channel types. Files, channels, and users require explicit `content_types`.

`sort="relevance"` maps to relevance ordering; `sort="timestamp"` returns newest first. Results preserve relevance order otherwise, are deduplicated, filtered against the read deny policy, capped at 20, and bounded to approximately 12,000 serialized UTF-8 bytes. A new query is required for another result set.

`include_context=True` is accepted and sent to Slack, but the current public projection does not expose nested context; each returned item has `context: null`. Do not assume contextual messages are available from the current tool.

Search returns normalized results. A malformed search response raises `SlackResponseError`; malformed individual records may be omitted. Search does not expose cursors.

### `slack.messages(conversation, *, after=None, before=None, limit=50)`

Reads one known conversation through bounded history. Resolve the conversation ID with `slack.conversations` first. `after` and `before` are Slack timestamp strings with six fractional digits and are exclusive. Results are newest first, deduplicated, and capped at 100. History does not expose cursors.

### `slack.thread(conversation, thread_ts, *, limit=100)`

Reads one known thread. `thread_ts` must be a positive Slack timestamp with exactly six fractional digits, normally obtained from a message's `thread_timestamp`. The parent is required and is returned first; replies follow in chronological order. Results are capped at 100 and do not expose cursors.

## Write functions

All writes require explicit write enablement and the independent write deny policy. Confirm externally visible actions with the user before calling.

### `slack.send_message(conversation, text, *, thread_ts=None, blocks=None)`

Sends to an existing permitted conversation. `text` must be non-empty and at most 40,000 characters. The exact caller text is preserved after the automatic prefix `:archie: `, including for replies. `thread_ts`, when supplied, is preserved byte-for-byte.

The optional `blocks` value supports bounded non-interactive Block Kit only:

- `header` with a required `plain_text` object, capped at 150 characters;
- `section` with `mrkdwn` text and/or 1–10 `mrkdwn` fields;
- `context` with 1–10 `plain_text` or `mrkdwn` elements;
- `divider`.

At most 50 blocks are accepted and serialized blocks are capped at 12,000 characters. Interactive actions, buttons, menus, accessories, inputs, images, attachments, metadata, and arbitrary block fields are rejected before provider access.

Successful results have exactly:

```json
{
  "conversation_id": "C123",
  "message_ts": "1712345678.000001",
  "thread_ts": null,
  "permalink": null
}
```

### `slack.reactions(conversation, timestamp)`

Reads the reactions on one known message. Use the conversation ID and exact message timestamp. The result has exactly this shape:

```json
{
  "conversation": "C123",
  "timestamp": "1712345678.000001",
  "reactions": [
    {"name": "thumbsup", "count": 2, "users": ["U123", "U456"]}
  ]
}
```

Reaction state is read for the current invocation and is not cached. `users` may not include every reacting user even when `count` is larger. Use this function to verify a reaction before repeating an uncertain mutation.

### `slack.react(conversation, timestamp, reaction, *, action="add")`

Adds or removes one reaction on a known message. `timestamp` must be a positive six-fraction Slack timestamp. `reaction` is a bare name of 1–50 ASCII letters, digits, `_`, `+`, or `-`; do not use surrounding colons or Unicode emoji glyphs. `action` is exactly `add` or `remove`.

Successful results have exactly:

```json
{
  "conversation": "C123",
  "timestamp": "1712345678.000001",
  "reaction": "thumbsup",
  "action": "add"
}
```

## Errors and retry policy

Errors are sanitized and do not contain credentials, message bodies, or hidden deny-list details.

- `SlackValidationError`: an argument or option is invalid. Correct it; do not retry unchanged.
- `SlackPolicyError`: the Slack surface or target is denied/disabled. Do not bypass policy or retry unchanged.
- `SlackConfigurationError`: credentials are missing or incomplete. Authenticate/configure Slack through Nexus.
- `SlackAuthenticationError`: Slack rejected or expired authentication. Reauthenticate through the configured Slack login flow; do not retry unchanged.
- `SlackReauthorizationRequiredError`: required OAuth capabilities are missing. Reauthorize with the requested capabilities; never replace or print the token.
- `SlackNotFoundError`: the canonical target could not be resolved. Discover permitted IDs again; do not guess names or provider syntax.
- `SlackResponseError`: Slack returned a malformed or rejected response. Do not automatically repeat a mutation.
- `SlackPaginationError` / `SlackDeadlineError`: bounded enumeration did not complete. Do not assume a partial result is complete.
- `SlackBoundedResultError`: the safe result bound was exceeded. Narrow the request; do not expect hidden records to be silently returned.
- `SlackRateLimitError`: Slack throttled the attempted method. No automatic retry or pagination continuation occurs. If `retry_after_seconds` is present, wait at least that long before a deliberate retry; if absent, do not guess an immediate delay. Retry only the original read operation, not a broader query.
- `SlackTransportError`: the network boundary failed. Reads may be retried after checking connectivity. A mutation may have been transmitted, so do not repeat it automatically.
- `SlackMutationIndeterminateError`: a send or reaction may have succeeded, or its success could not be validated. Verify the target before repeating; never run an automatic retry loop. Reaction state may not be directly verifiable through the available tools.

## Safe operating patterns

### Find a person and read a DM

```python
async def main():
    users = await slack.users(query="Henry Dennis")
    conversations = await slack.conversations(types=["dm"])
    # Match the permitted participant identity locally and use the returned DM id.
    return {"users": users, "conversations": conversations}
```

Then read the selected conversation:

```python
messages = await slack.messages("D123", limit=5)
```

### Read a thread

```python
async def main():
    messages = await slack.messages("C123", limit=10)
    threaded = [item for item in messages if item.get("thread_timestamp")]
    if not threaded:
        return {"thread": None}
    return await slack.thread("C123", threaded[0]["thread_timestamp"], limit=20)
```

Use the conversation ID and the thread timestamp together. If a search or history result does not identify a thread, do not guess a timestamp.

### Search context

```python
results = await slack.search(
    "project update",
    channel_types=["public_channel", "private_channel"],
    sort="timestamp",
    include_context=False,
    limit=10,
)
```

### Confirm before sending

Only call this after the user confirms the target, exact text, optional thread, and blocks:

```python
result = await slack.send_message(
    "C123",
    "Deployment is complete.",
    blocks=[
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Deployment complete*"}},
        {"type": "divider"},
    ],
)
```

## Unsupported capabilities

Workspace administration, channel creation/management, membership changes, file upload, Canvas, pins, bookmarks, reminders, saved-item management, message editing/deletion, arbitrary Slack API methods, incoming webhooks, raw search cursors, raw provider payloads, interactive Block Kit actions, and credential handling are unsupported.
