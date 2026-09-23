# 042 — Project: Slack personal assistant

## Objective

Replace the first-generation Slack wrapper with a focused personal-assistant interface for Archie. The project provides bounded context retrieval, conversation and user discovery, message and thread reading, and user-authorized message and reaction actions through seven Slack functions.

The work starts from a fresh branch from `main`. The old Slack aliases and provider-shaped public functions are removed rather than retained for compatibility.

## Context

The existing Slack tool is not in a useful product state for a personal assistant: discovery is channel-first, identity information is inconsistent, search relies on the legacy API, and pagination and ordering make bounded context difficult to consume. The redesign is for an internal Slack app acting as the authenticated user, not for workspace administration or data export.

Slack Real-time Search is the required search surface for AI context retrieval. `users.conversations` is the basis for discovering conversations in which the authenticated user participates. The project is split into the M1–M7 work items, with M1 owning the shared Slack foundation and `slack.search`.

## Locked Decisions

1. **D1: Fresh start and public surface** — Begin from a fresh branch from `main`. Remove old aliases and old public Slack functions; provide no compatibility aliases. The public functions are `slack.search`, `slack.conversations`, `slack.users`, `slack.messages`, `slack.thread`, `slack.send_message`, and `slack.react`.
2. **D2: Personal-assistant boundary** — The tool acts for the authenticated Slack user. Its scope is finding context, understanding discussions, resolving people and conversations, reading bounded context, sending messages, and reacting to messages. Administration, channel management, file upload, Canvas, pins, bookmarks, reminders, and saved-item management are out of scope.
3. **D3: RTS is required** — `slack.search` uses `assistant.search.context` exclusively. It never falls back to legacy message search.
4. **D4: Search defaults** — Search returns message results by default; files require an explicit opt-in. It searches accessible public channels, private channels, DMs, and group DMs. Contextual messages are included by default (`context: true`).
5. **D5: Search ordering and bounds** — Search defaults to relevance ordering and supports explicit timestamp ordering. Each request makes one RTS request, returns at most 20 results, exposes no cursor, performs no pagination, and returns no pagination metadata. A new query is required for a different result set.
6. **D6: Search time boundaries** — Search `after` and `before` filters are exclusive ISO-8601 boundaries.
7. **D7: Conversation discovery** — `slack.conversations` uses `users.conversations`, returns conversations the authenticated user participates in, excludes archived conversations, and never excludes muted conversations. It supports channel, DM, and group-DM filters.
8. **D8: User discovery** — `slack.users` uses the full user cache as its source, filters bots from returned results, and supports fuzzy matching over name, real name, display name, and optionally email according to the M2 work-item contract.
9. **D9: Message bounds and ordering** — `slack.messages` reads known conversations, supports exclusive ISO-8601 `after` and `before` boundaries, accepts a limit from 1 through 100, uses a bounded recent default when no time bound is supplied, remains bounded for large windows, and returns messages newest first.
10. **D10: Thread ordering** — `slack.thread` returns the thread parent first, followed by replies chronologically.
11. **D11: No message cache** — Message and thread content is never persisted in the metadata cache. Search results are not cached either.
12. **D12: Flat deny lists** — Slack policy has independent flat `read.deny` and `write.deny` lists under the Slack tool configuration. Channel-name entries use `#name`; conversation IDs use exact case-insensitive `C...`, `G...`, or `D...` matches; other entries identify one-to-one DM participants by exact case-insensitive username, real name, display name, or email. Group DMs are denied by conversation ID, not participant lists.
13. **D13: Deny-list enforcement** — Deny entries are validated and resolved fail-closed. Direct denied targets are blocked before the Slack API call. Broad search may query Slack and then locally filters denied results and context; denied content, identities, participants, and counts are not returned or disclosed.
14. **D14: Metadata caching** — Persist only normalized metadata for users and user-participating conversations. User metadata has a 24-hour TTL; conversation metadata has a 15-minute TTL. Refresh bypasses the relevant cache. Missing, expired, corrupt, or incomplete caches trigger a full refresh, with no stale fallback.
15. **D15: Safe metadata scope** — Cache metadata includes freshness and completeness information and is isolated under the configured Archie home directory with restrictive, concurrent-agent-safe storage. Partial enumeration is never presented as complete.
16. **D16: Writes and policy** — `slack.send_message` sends to an existing permitted conversation and may reply using a thread timestamp. `slack.react` adds or removes a reaction on a known message. Writes require explicit Slack write enablement and are checked against the independent write deny list before provider access.
17. **D17: Rate limits** — Rate-limit responses are surfaced immediately as sanitized errors with the method and valid `Retry-After` seconds when available. No automatic wait, retry, pagination continuation, or cross-agent cooldown coordination occurs.
18. **D18: Internal app** — The integration is an internal Slack app using a user token and is not a general workspace automation or export API.

## Design

- **Operation, client, and errors:** Every public operation runs through a shared Slack operation context carrying the immutable session policy, invocation deadline, credential/client boundary, and sanitized error mapper. The client is an async provider boundary, not a public tool and not a policy layer. Every M1–M7 work item consumes the authoritative policy shape unchanged. The shared taxonomy distinguishes validation, policy, authentication/configuration/scope, resolution/not-found, malformed provider response, transport/deadline, pagination/incomplete enumeration, cache, rate-limit, and indeterminate mutation failures. Raw response bodies, tokens, secret-bearing URLs, stack traces, deny entries, hidden identities, and hidden counts never cross the boundary.
- **Rate-limit parsing:** The shared `Retry-After` value has type `int | None`. HTTP 429 and Slack rate-limit envelopes become immediate `SlackRateLimitError` values containing only the attempted method and this value. Parse only an integer number of seconds greater than or equal to zero; missing or invalid values (including negative, fractional, date, or otherwise non-integer forms) become `None`. It never sleeps, retries, continues pagination, or serves stale data.
- **Policy contract:** The one authoritative Slack policy shape, consumed unchanged by every M1–M7 work item, is `{enabled: bool, read: {enabled: bool, deny: list[str]}, write: {enabled: bool, deny: list[str]}}`. Its defaults are `{enabled: true, read: {enabled: true, deny: []}, write: {enabled: false, deny: []}}`; no alternate or nested policy shape is permitted. The lists are independent and flat: entries are strings, validated and resolved fail-closed. `#name` addresses a channel; `C...`, `G...`, or `D...` addresses an exact conversation ID case-insensitively; every other entry addresses one-to-one DM participants by exact case-insensitive username, real name, display name, or email. Group DMs never resolve participant-list entries. Read and write snapshots are independent, and direct authorization precedes the relevant provider operation.
- **Cache contract:** Metadata uses one shared Archie-home cache namespace with explicit partitions for normalized users and normalized participating conversations; the partitions have separate schema versions, locks, and TTLs. A record includes `schema_version`, successful-refresh `fetched_at`, freshness/expiry timestamps, `complete`, and only normalized records. Timestamps are timezone-aware UTC values (and are compared with the project clock/monotonic deadline seams). Writes are atomic and restrictive; missing, corrupt, wrong-version, expired, incomplete, or partially refreshed records are cache misses. There is no stale fallback. No message, thread, search, reaction, raw provider payload, credential, or policy-decision cache exists.
- **Bounds:** Public result caps are search 20 and messages 1–100; each operation’s M1–M7 work-item contract may choose its locked default within those bounds. Provider page sizes never exceed the remaining public result budget. Cursor enumeration has finite page and operation-deadline ceilings; the shared project ceiling is 100 pages and 60 seconds unless a stricter M1–M7 work-item cap applies. Repeated/malformed cursors, cap exhaustion, or deadline expiry is a typed incomplete/pagination failure, never a successful partial directory. Result-size caps apply after authorization, normalization, deduplication, and ordering; cursors and pagination metadata are never model-visible.
- **Credentials and scopes:** Credentials remain in the existing typed runtime credential path and are never arguments, results, cache values, or documentation examples. The project uses one authenticated Slack user token and one shared async client boundary; it does not create a second credential store or action token. M1–M7 use the same credential and client boundary. Required OAuth scopes are capability-specific and validated in resolver preflight before provider use. Resolver preflight determines the required read scopes from the target type; a send or reaction operation uses the union of the resolver’s required read scopes and its applicable write scope. This includes RTS search scopes for requested content/channel types, `users:read` (and email scope only when email matching is requested), conversation-discovery capability, history/replies read capability, and write/reaction capabilities. Missing capability is a sanitized configuration/authentication error, not a legacy fallback.
- **Normalized model boundaries:** Provider payloads end at the client/adapter boundary. The shared normalized identity model contains only safe user ID, username, real name, display name, optional authorized email, and status flags needed for filtering and joins. The normalized conversation model contains canonical ID, type, safe name/flags, and participant ID references, never message content. M1 foundation owns one canonical internal `Message` schema for every read path with exactly these fields: `id`, `conversation_id`, `ts` (the Slack canonical timestamp string), `author`, `text`, `subtype`, `thread_ts`, `reply_count`, and `permalink`. `slack.messages` and `slack.thread` consume this schema and may define public projections only when each renamed field is explicitly mapped to one canonical field; they may not redefine or replace it. Messages and threads are invocation-only and never persisted. Public projections cannot expose raw provider fields.
- **Exact-message lookup:** M1 foundation owns the shared exact-message resolver contract for resolving a known message by the exact canonical key `(conversation_id, ts)`, including authorization, normalization, and sanitized not-found/error behavior. Its resolver preflight determines the required read scopes from the target type before provider access. `slack.react` consumes this seam and does not define a separate provider lookup or message model.
- **Registration and skill ownership:** M1 owns the central Slack namespace registration, removal of obsolete aliases/provider-shaped functions, the exact seven-function allowlist, shared configuration/credential wiring, and the complete shared Slack skill guidance. M2–M7 may append focused contract sections to that guidance, but may not replace, fork, or relocate M1’s foundation. No M1–M7 work item may register a second Slack namespace, duplicate the credential path, or teach raw provider calls.

## Milestones

1. **M1 — Shared Slack foundation and `slack.search`**
   - **Objective:** Establish the shared Slack foundation and deliver bounded, policy-filtered context retrieval through `slack.search`.
   - **Boundary:** M1 owns the complete shared foundation required by search: operation context, authoritative policy schema and enforcement, async client and sanitized errors, cache identity/model seams, conversation and user lookup contracts, exact-message lookup, central registration, and complete shared skill guidance. It then delivers bounded message-default and explicit-file context retrieval through required RTS with relevance or timestamp ordering and exclusive time filters. Users and conversations consume these seams rather than redefining them.
   - **Owner/exercises:** D1–D6, D11–D18; the central seven-function registration, all shared contracts including the exact-message resolver consumed by M7, and broad-search deny filtering.
   - **Depends on:** None. M1 owns both the foundation and search, rather than being a foundation-only phase.
2. **M2 — `slack.users`**
   - **Objective:** Provide complete, normalized, bot-free user discovery backed by the shared user cache.
   - **Boundary:** Consume M1’s user-cache, identity, policy, operation, client, error, registration, and skill seams for full user-cache-backed lookup with complete normalized identity snapshots, bot exclusion, and specified fuzzy matching.
   - **Owner/exercises:** D1, D8, D12–D15, D17–D18; user lookup behavior and the shared user-cache partition, without replacing M1 contracts.
   - **Depends on:** M1 shared foundation, registration, policy, cache, credential, error, and user lookup seams.
3. **M3 — `slack.conversations`**
   - **Objective:** Discover and locally filter the authenticated user’s complete, permitted conversation set.
   - **Boundary:** Consume M1’s conversation, user, policy, operation, client, error, registration, and skill seams for discovery and local filtering of the authenticated user’s complete, non-archived conversations, including channel, DM, and group-DM types.
   - **Owner/exercises:** D1, D7, D12–D15, D17–D18; conversation lookup behavior and the shared conversation-cache partition, without replacing M1 contracts.
   - **Depends on:** M1, M2.
4. **M4 — `slack.messages`**
   - **Objective:** Deliver bounded, newest-first message history for a known permitted conversation.
   - **Boundary:** Consume M1’s canonical normalized `Message`, conversation/user lookup, policy, operation, client, error, registration, and skill seams for bounded newest-first history in a known permitted conversation, with exclusive time bounds and recent defaults.
   - **Owner/exercises:** D1, D9, D11–D15, D17–D18; the messages public projection and bulk author joins, without redefining the canonical message model.
   - **Depends on:** M1, M2, M3.
5. **M5 — `slack.thread`**
   - **Objective:** Deliver bounded thread context with the parent first and replies in chronological order.
   - **Boundary:** Consume M1’s canonical normalized `Message` and shared lookup seams for bounded thread retrieval with the parent first and replies in chronological order.
   - **Owner/exercises:** D1, D10–D15, D17–D18; the thread public projection, without redefining the canonical message model.
   - **Depends on:** M1, M2, M3, M4.
6. **M6 — `slack.send_message`**
   - **Objective:** Enable policy-gated messages to existing permitted conversations, including thread replies.
   - **Boundary:** Policy-gated message creation in an existing permitted conversation, including optional thread replies.
   - **Owner/exercises:** D1, D12–D13, D16–D18; mutation outcome and indeterminate-result handling.
   - **Depends on:** M1, M2, M3, M4, M5.
7. **M7 — `slack.react`**
   - **Objective:** Enable policy-gated reaction additions and removals on known permitted messages.
   - **Boundary:** Consume M1’s exact-message lookup, canonical normalized `Message`, write-policy, client, error, registration, and skill seams for policy-gated addition or removal of a reaction on a known permitted message.
   - **Owner/exercises:** D1, D12–D13, D16–D18; mutation outcome and indeterminate-result handling, without defining a separate message lookup or model.
   - **Depends on:** M1, M2, M3, M4, M5, M6.

## Sequencing

1. Establish the replacement public surface, central registration, shared skill ownership, credential/scope wiring, and cross-cutting contracts from a fresh branch from `main` as part of M1.
2. Complete M1’s shared foundation and `slack.search`, including RTS capability validation, bounded results, policy filtering, and sanitized errors.
3. Deliver M2 `slack.users` and publish the complete normalized user cache and identity resolver.
4. Deliver M3 `slack.conversations`, using users for participant joins and establishing complete normalized conversation metadata and resolution.
5. Deliver M4 `slack.messages` after M1, M2, and M3.
6. Deliver M5 `slack.thread` after M1, M2, M3, and M4.
7. Deliver M6 `slack.send_message` after M1, M2, M3, M4, and M5.
8. Deliver M7 `slack.react` after M1, M2, M3, M4, M5, and M6.
9. Replace obsolete Slack guidance and validation, verify the seven-function registration and shared contracts, and validate the complete project against all locked decisions.

## Status

| Work item | Status |
|---|---|
| M1 shared foundation + `slack.search` | planned |
| M2 `slack.users` | blocked by M1 |
| M3 `slack.conversations` | blocked by M1, M2 |
| M4 `slack.messages` | blocked by M1–M3 |
| M5 `slack.thread` | blocked by M1–M4 |
| M6 `slack.send_message` | blocked by M1–M5 |
| M7 `slack.react` | blocked by M1–M6 |

## Open decisions

- Exact public parameters, result envelopes, identity fields, and model-visible formatting.
- The exact file-search opt-in shape for `slack.search`.
- The exact fuzzy-matching algorithm and deterministic ordering for `slack.users` and `slack.conversations`.
- The exact bounded recent window for `slack.messages`.
- Permalink behavior when provider responses omit permalinks.
- Reaction-name validation and idempotency behavior.
- Capability-specific validation and error wording that do not alter the locked boundaries above.
