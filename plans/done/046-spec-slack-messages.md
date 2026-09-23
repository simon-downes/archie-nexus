# 046 — Spec: `slack.messages`

## Objective

Implement `slack.messages` as Archie’s bounded history reader for one known, permitted Slack conversation. It MUST authorize the target before `conversations.history`, apply precise exclusive UTC bounds, retrieve no more than the requested number of messages, and return a safe normalized projection newest first. It is context retrieval, not export, search, or a message cache.

## Context

Project `042-project-slack-personal-assistant.md` defines this as M4 of the replacement seven-function Slack surface. The dependency is explicit: M1 owns the shared `OperationContext`, read policy, authenticated-user credential and scope validator, async Slack client/transport, sanitized error taxonomy, cache interfaces, canonical normalized message/user model, exact-message resolver, central `slack` namespace registration, and shared skill guidance. M2 owns the normalized 24-hour user snapshot and lookup/refresh seam; M3 owns the 15-minute conversation snapshot and `resolve_conversation_reference` seam. This spec is blocked until those seams and their tests are implemented and green; it consumes them and MUST NOT reimplement, fork, or take ownership of them. Slack is accessed through the authenticated user token and old provider-shaped functions and aliases are removed.

The public operation accepts a known conversation ID or a reference accepted by the shared resolver; it MUST NOT discover arbitrary conversations as a side effect of reading history. Conversation metadata may come from the complete 15-minute conversation cache/refresh seam. User enrichment uses the complete 24-hour normalized user cache/refresh seam. Message content and message results are invocation-only.

Slack history uses `oldest`, `latest`, and cursors. Provider inclusivity is not trusted: `after` excludes timestamps `<= after`, and `before` excludes timestamps `>= before` after local parsing. With neither bound, the effective window is the exclusive rolling 24 hours ending at one captured invocation instant. All public timestamps are canonical UTC ISO-8601.

## Requirements (RFC 2119)

Each requirement is normative. Acceptance criteria below are part of the contract.

### Public API and argument validation

- The only public history function MUST be:

  ```python
  async def messages(
      conversation: str,
      *,
      after: str | None = None,
      before: str | None = None,
      limit: int = 50,
  ) -> list[dict]
  ```

- `conversation` MUST be a non-empty trimmed string of at most 200 Unicode code points. The trimmed value MUST be passed to `resolve_conversation_reference`; this spec adds no reference syntax. The resolver’s documented ID, channel/name, and DM participant forms are the only accepted forms.
- A supplied `after` or `before` MUST match exactly `YYYY-MM-DDTHH:MM:SS[.fraction]Z` or `YYYY-MM-DDTHH:MM:SS[.fraction]+00:00`, where `fraction` contains 1–6 decimal digits. The separator MUST be `T`; lowercase `z`, spaces, date-only values, offsets other than `+00:00`, naive values, booleans, non-strings, malformed dates, and more than six fractional digits MUST be rejected locally. Parsing MUST retain microsecond precision, padding a shorter fraction with zeros. Parsed bounds MUST be at or after `1970-01-01T00:00:00.000000Z`; pre-epoch bounds are rejected locally because Slack history timestamps cannot represent them safely.
- Bounds are exclusive. If both are supplied, `after` MUST be strictly earlier than `before` by the parsed microsecond instants. Equal or reversed bounds MUST be rejected before policy, resolution, credentials, or provider access.
- If neither bound is supplied, the service MUST read its injected UTC clock exactly once, truncate that instant to microseconds, set `before` to it and `after` to exactly 24 hours earlier. The effective window is `(now - 24h, now)`. Paging MUST reuse these immutable instants. If one bound is supplied, the other remains unbounded; no 24-hour bound is added.
- `limit` MUST be a real `int`, not `bool`, in the inclusive range 1–100; the default is 50. Invalid arguments MUST fail locally and make no Slack Web API request.
- Local validation errors MUST be sanitized and MUST disclose neither tokens, provider data, deny entries, nor hidden-target existence.

### Conversation resolution and policy ordering

- The immutable Slack read-policy snapshot MUST be checked before credentials/provider access. Disabled reads raise the shared sanitized policy error.
- After argument validation and read-policy gating, the service MUST call the shared `resolve_conversation_reference` seam. Its return contract is `ResolvedConversation | None`, where `ResolvedConversation` contains exactly the canonical `id`, canonical `type` (`public_channel`, `private_channel`, `im`, or `mpim`), `is_archived: bool`, and opaque safe `policy_identity` used by the shared deny evaluator (no names, participants, raw metadata, or provider payload). `None` represents unresolved/ambiguous/invalid without revealing which. Resolution MUST return one canonical conversation ID and safe metadata, including archived status and policy identity. Resolution may load a complete conversation metadata snapshot or perform its complete refresh; it MUST NOT call `conversations.history`.
- The resolved target MUST be rejected as unresolved/ambiguous/invalid, archived, or directly denied, using the shared sanitized not-found/validation/policy category as appropriate. Deny evaluation MUST occur before the first history call. A denied target MUST not be probed through history. No conversation may be joined, opened, created, or substituted.
- The history request MUST use only the resolved canonical ID. Resolver refresh calls are permitted metadata calls and do not weaken the no-history-before-authorization guarantee.

### Provider mapping, timestamps, and bounded pagination

- The adapter MUST call `conversations.history` with the canonical ID, `limit <= public limit`, and omit absent optional parameters. `after` maps to `oldest`; `before` maps to `latest`; default bounds map identically. It MUST never swap, broaden, or rewrite bounds.
- Slack timestamp conversion MUST use decimal seconds with exactly six fractional digits (`SSSSSS.ffffff`) derived from the UTC microsecond instant. Epoch seconds MUST be integer seconds; negative or non-finite values are invalid. The adapter MUST use the same conversion for both bounds. Local comparisons use the original microsecond instants, not rounded outbound values.
- The adapter MUST request Slack’s newest-first history mode (`conversations.history` returns newest-first; no client-side `sort` parameter exists) and MUST validate/assume that each page is non-increasing by parsed timestamp. If a page violates newest-first order, or a later page contains a timestamp newer than the oldest timestamp accepted from the preceding page, it MUST raise sanitized `SlackPaginationError` rather than rely on an unbounded scan. The service MUST still sort the final list by `(timestamp_instant descending, canonical_tie_key ascending)`; provider order is never a public ordering contract. The adapter MAY stop once the oldest observed timestamp is at or before the exclusive `after` bound, but MUST not use that optimization when `after` is absent.
- Each page request MUST use `min(100, remaining_public_result_budget)` as its page size; it MUST never send a page size greater than the public `limit` or remaining budget. `remaining_public_result_budget` is the number of output slots still needed, and MUST NOT be reduced for out-of-window, duplicate, or otherwise non-returned candidates; this preserves bounded-selection correctness. The adapter MUST follow `response_metadata.next_cursor` only while it may still need results. It MUST stop immediately when the budget is full, the cursor is absent/blank, or the provider explicitly returns an empty terminal page. An empty page with a nonblank cursor is malformed/incomplete (not a successful empty result), and a non-empty page with a blank cursor is a complete terminal page.
- Concrete safety bounds are mandatory: at most 100 provider pages, at most 100 records per page, at most 10,000 candidate records per invocation, and a 60-second monotonic operation deadline. Exceeding a bound, deadline expiry, repeated cursor, non-string/non-bounded cursor, or incomplete pagination MUST raise the corresponding sanitized pagination/deadline error, never silently return partial data. A short page with a nonblank cursor is not complete.
- A successful Slack envelope MUST contain exactly the provider fields `ok`, `messages`, and optionally `response_metadata` (unknown top-level fields are malformed); `ok` MUST be the boolean `true`, `messages` MUST be a list, and `response_metadata` MUST be absent or an object. `response_metadata.next_cursor` MUST be absent, blank, or a non-empty string of at most 512 code points. An explicit `ok: false`, malformed envelope/page, or malformed message is a sanitized provider-response error.
- Every candidate MUST have a valid Slack timestamp and string text; invalid timestamps or missing/non-string text are malformed provider data and MUST fail the operation, not be skipped or guessed. Slack `ts` MUST be a string matching `^[0-9]+\\.[0-9]{1,6}$`, with a non-negative integer epoch part and at most microsecond precision; parse it to a UTC microsecond instant (right-pad the fraction). Strict local filtering excludes `timestamp <= after` and `timestamp >= before`. A pre-Unix-epoch instant (negative epoch or equivalent negative parsed value) MUST be rejected before filtering; it MUST never be formatted or compared as a valid message.
- Deduplication MUST use the canonical identity key `(timestamp_instant, stable_id)`, where `stable_id` is a validated non-empty provider `ts`/message ID when available and otherwise the canonical six-fraction timestamp string. An exact repeated key is one message, retaining the first valid occurrence. If different stable IDs share a timestamp, both are retained and ordered by stable ID ascending. A provider message appearing on overlapping pages MUST therefore appear once. The final list MUST contain at most `limit` records.
- Cursors and provider pagination metadata MUST never be returned.

### Canonical internal message mapping and public schema

The canonical internal message model is owned by M1. Every provider message MUST first be normalized to exactly these internal fields (nullable values are present as `None`):

```python
{
    "id": str | None,
    "conversation_id": str,
    "ts": str,                    # YYYY-MM-DDTHH:MM:SS.ffffffZ
    "author": dict,
    "text": str,
    "subtype": str | None,
    "thread_ts": str | None,
    "reply_count": int | None,
    "permalink": str | None,
}
```

The public result is a deliberate projection of that internal model, not a second internal model. Its exact mapping is: `id` → `id`, `conversation_id` → omitted (the requested conversation is implicit), `ts` → `timestamp`, `author` → `author`, `text` → `text`, `subtype` → `type` (using `message` when `subtype` is `None`), `thread_ts` → `thread_timestamp`, `reply_count` → `reply_count`, and `permalink` → `permalink`. Every returned item MUST be JSON serializable and have exactly these public keys (nullable values are present as `None`):

```python
{
    "id": str | None,
    "type": str,
    "timestamp": str,                 # YYYY-MM-DDTHH:MM:SS.ffffffZ
    "author": {
        "id": str | None,
        "username": str | None,
        "real_name": str | None,
        "display_name": str | None,
        "is_deleted": bool | None,
        "is_bot": bool | None,
    },
    "text": str,
    "thread_timestamp": str | None,
    "reply_count": int | None,
    "permalink": str | None,
}
```

- `timestamp` and a valid `thread_timestamp` MUST be canonical UTC strings with six fractional digits and `Z`; provider precision is retained through microseconds. `thread_ts`, when present, MUST use the same non-negative Slack timestamp grammar and is malformed if invalid. `id` MUST be a non-empty string of at most 256 Unicode code points containing only safe printable identifier characters `[A-Za-z0-9._:-]` when provider `ts`/ID is present; otherwise it MUST be the canonical six-fraction UTC timestamp string. It is never omitted merely because Slack omitted an ID. The identity/tie key is `(parsed timestamp instant, provider ID or canonical timestamp)`.
- Ordinary messages normalize to `type="message"` only when `subtype` is absent. If present, `subtype` MUST be a non-empty string of at most 64 code points and MUST be in the explicit allowlist `{`message_changed`, `message_deleted`, `channel_join`, `channel_leave`, `channel_topic`, `channel_purpose`, `channel_name`, `group_join`, `group_leave`, `group_topic`, `group_purpose`, `group_name`}`; it normalizes to that exact allowlisted name. Unknown, empty, oversized, or non-string subtypes MUST raise a sanitized provider-response error. Raw subtype, blocks, attachments, files, reactions, edit history, metadata, and envelopes MUST never escape.
- `text` MUST be a safe normalized string of at most 40,000 Unicode code points. It MUST not be synthesized from raw payload fields. Apply the shared M1 safe-text normalizer exactly: accept only a provider string, reject control characters other than tab/newline/carriage return, normalize CRLF/CR to LF, preserve ordinary Unicode and Slack mrkdwn as text, and never interpret, expand, or synthesize links/mentions from blocks or other fields. Raw provider markup/objects are not returned. Text over 40,000 code points is malformed (never silently truncated).
- `author` MUST be joined by provider user ID through the shared normalized-user lookup seam. The service MUST perform one bulk lookup for the distinct author IDs (or one cache snapshot lookup), never `users.info` per message. A missing author remains visible with its stable ID and null human/status fields. Bot/deleted status does not hide a message.
- `reply_count` MUST be a non-negative real integer when supplied, otherwise `None`; replies MUST NOT be fetched to calculate it. `permalink` is retained only if it is a bounded safe absolute HTTPS URL with scheme exactly `https`, a non-empty host, no username/password, query allowed, no fragment, no control characters, no whitespace, and at most 2,048 code points; otherwise it is `None`. No permalink request is made per message.
- Optional string fields are bounded to 256 code points; IDs are bounded to 256. Oversized provider fields are malformed provider data unless the shared normalization contract explicitly truncates that field; this spec does not silently truncate message text. An empty successful history returns `[]`.

### Cache, user lookup, errors, scopes, and replacement

- Message content, provider messages, normalized message results, and pagination state MUST NOT be cached, persisted, logged, or placed in metadata caches. The only permitted enrichment seam is `load_users_snapshot()`/`lookup_users(ids)`, which reads the normalized 24-hour user cache and performs one complete `users.list` refresh when missing, expired, corrupt, incomplete, or wrong-version; it MUST never issue per-message `users.info`. Refresh MUST be transactional: only a complete, validated snapshot replaces the prior snapshot; refresh failure MUST raise the shared cache/provider error and MUST NOT use stale data or return partial users. The service calls this seam once for the distinct author IDs (an empty ID set performs no refresh). Conversation resolution owns its separate 15-minute cache. Cache writes contain metadata only.
- The operation MUST require the existing authenticated user token and validate required capability before `conversations.history`: `channels:history` for public channels, `groups:history` for private channels, `im:history` for one-to-one DMs, and `mpim:history` for group DMs. Scope selection MUST use the resolver’s canonical conversation type, not user input; a missing or unknown type is a sanitized resolution/configuration failure. Missing capability raises sanitized `SlackReauthorizationRequiredError` listing only missing scope names; it makes no history request. Credentials are never arguments, results, cache values, or documentation examples. Transport is the shared async Slack client: HTTPS `POST https://slack.com/api/conversations.history`, JSON body containing only `channel`, `limit`, and present `oldest`/`latest`/`cursor`, `Authorization: Bearer <existing user token>`, and `Content-Type: application/json`; the token and raw response are never logged or exposed, and the adapter performs no retries.
- HTTP 429 or a Slack rate-limit envelope MUST immediately raise `SlackRateLimitError` with exactly `method="conversations.history"` and `retry_after_seconds: int | None`; the envelope must not expose any other provider fields. The shared M1 Retry-After resolver MUST be used: it checks the HTTP `Retry-After` header first, otherwise the Slack `retry_after` field, and accepts only an integer number of seconds (surrounding ASCII whitespace is allowed). Missing, fractional, date, scientific-notation, non-finite, negative, malformed, or otherwise non-integer values resolve to `None`. The error MUST NOT sleep, retry, continue pagination, use stale messages, or coordinate a cooldown.
- Authentication, scope, policy, validation, resolution/not-found, malformed-envelope, malformed-message, transport, deadline, pagination/incomplete, cache, and rate-limit failures MUST remain distinct sanitized shared error categories. Raw response bodies, tokens, secret URLs, message text, denylist entries, hidden existence, and hidden counts MUST not escape.
- The replacement public name is `slack.messages`; `get_history` and every obsolete history alias/registration MUST be absent. The central Slack registration owner is the shared Slack foundation/`slack/__init__.py`, which owns one namespace and the exact seven-function allowlist: `search`, `conversations`, `users`, `messages`, `thread`, `send_message`, and `react`. This slice owns only the `messages` implementation, its focused tests, and focused documentation; it MUST NOT register a second namespace, duplicate credential/scope wiring, or remove/replace another slice’s registration.
- Slack skill guidance MUST state the exact signature, UTC grammar and exclusive bounds, 24-hour default, one-sided behavior, 1–100 limit, newest-first order, schema/limits, authorization order, user-cache seam/no message cache, required scopes, bounded paging, and immediate rate-limit behavior. It MUST not teach raw API calls or credentials.

### Acceptance criteria

- **AC1:** A valid direct ID resolves to one canonical ID, validates scope and deny policy, and only then calls history; denied, unresolved, ambiguous, archived, invalid, or read-disabled targets make zero history calls.
- **AC2:** Signature tests cover default 50, accepted 1/100, rejection of 0/101/booleans, the exact UTC grammar, six-digit normalization, rejection of naive/non-UTC/malformed/over-precision bounds, and equal/reversed bounds.
- **AC3:** A clock spy proves the no-bound default uses one instant and exactly `(now-24h, now)`; one-sided calls send only the supplied bound.
- **AC4:** Request spies prove canonical ID, `after→oldest`, `before→latest`, six-fraction Slack conversion, required scope validation, and page sizes never exceed remaining budget or 100.
- **AC5:** Multi-page fixtures prove the 100-page/10,000-candidate/60-second bounds, early stop at a full budget, cursor validation/repetition detection, and no cursor leakage.
- **AC6:** Boundary, subsecond, overlap, same-timestamp, and out-of-order fixtures prove strict exclusion, canonical dedup, timestamp-descending order, and stable-ID tie order.
- **AC7:** Snapshot tests assert the exact schema, six-fraction UTC timestamps, safe text/subtype/permalink limits, thread/reply behavior, bulk user-cache lookup/refresh seam, and nullable unknown authors with no `users.info` calls.
- **AC8:** Empty history returns `[]`; malformed envelopes/pages/messages, transport/deadline/pagination failures, explicit provider errors, and incomplete pagination are typed sanitized failures, not partial success.
- **AC9:** 429 fixtures prove immediate failure with only method and valid non-negative retry details, no sleep/retry/continuation/stale fallback, and no message-cache read/write.
- **AC10:** Scope fixtures prove each conversation type requires its exact history scope and that missing scopes prevent provider access. Registration tests prove only the central seven-function owner registers the replacement and `get_history` is absent. Documentation tests assert the final contract.

## Technical Design

### Overview and ownership

Use a thin `conversations.history` adapter and a message service/formatter. The adapter owns request construction, exact timestamp conversion, required-scope check at the provider boundary, envelope/page validation, cursor paging, page/candidate/deadline guards, and provider-to-sanitized-error mapping. The service owns argument validation, one-clock default derivation, read policy, conversation resolution/deny ordering, user-cache bulk join/refresh, strict filtering, canonical deduplication, ordering, and exact projection. Neither layer persists message content.

The shared operation context supplies immutable policy, authenticated client, monotonic deadline, UTC clock, resolver, normalized metadata loaders, and error mapper. Provider payload models remain private to the adapter.

### Suggested components and seams

- `agent/src/archie_agent/exec/tools/slack/messages.py`: public function, validation, orchestration, filtering, ordering, and projection.
- `agent/src/archie_agent/exec/tools/slack/client.py`: shared async `conversations.history` boundary, scope check, timestamp mapping, envelope validation, bounded paging, and rate-limit parsing.
- `agent/src/archie_agent/exec/tools/slack/models.py`: private provider message and safe normalized projection models.
- Shared policy/conversation resolver: read gate, canonical resolution, archived status, and flat denylist.
- Shared user cache: `load_users_snapshot()` and `lookup_users(ids)` with 24-hour freshness and complete-refresh semantics; no message cache seam exists.
- Central Slack registration/skill owner: seven-function namespace and shared guidance; this slice adds only focused `messages` content.
- `tests/test_slack_messages.py` plus shared registration/scope/policy tests: exercise public and adapter seams with fake clock, deadline, resolver, cache, client, and provider; no network.

### Data flow

1. Validate all arguments and parse exact UTC bounds; read the clock once only for the default window.
2. Check read policy, resolve the conversation, reject archived/denied targets, and validate the exact required history scope.
3. Call the adapter with immutable instants and `remaining=limit`; page with the concrete guards and stop rules.
4. Validate and collect candidates, apply precise local bounds, deduplicate by `(instant, stable_id)`, bulk-load users, sort, and project at most `limit` records.
5. Return `[]` or the exact schema. No message cache, result cache, permalink lookup, reply lookup, retry, or stale fallback is permitted.

### Time and safety details

Canonical public timestamps always use `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Outbound Slack timestamps always use integer epoch seconds plus six fractional digits. The unrounded parsed instant remains authoritative for strict comparisons. Named constants MUST be `PAGE_SIZE=100`, `MAX_PAGES=100`, `MAX_CANDIDATES=10_000`, and `OPERATION_DEADLINE_SECONDS=60`; tests MUST exercise each.

## Milestones

### M4.1 — Bounded `conversations.history` provider seam

**Dependency gate:** M1+M2+M3 MUST already be implemented and verified. This milestone consumes their client, context, error, scope, transport, exact-message resolver, and normalized-model seams; it does not establish or modify them.

**Approach**

- Extend the shared async Slack client with exact `oldest`/`latest` conversion, required history-scope validation, envelope/message validation, immediate rate-limit parsing, cursor safety, `PAGE_SIZE`, `MAX_PAGES`, `MAX_CANDIDATES`, and monotonic deadline enforcement.
- Keep all pagination state per invocation. Return a complete validated candidate set or a typed failure; never write a message cache.

**Wiring**

- **State:** canonical conversation ID, immutable parsed bounds, remaining budget, seen cursors, page count, candidate count, and deadline.
- **Producers:** each valid page contributes at most 100 validated provider messages and one next cursor.
- **Consumers:** the service consumes only a complete bounded adapter result.
- **Call site:** `await slack_client.conversations_history(conversation_id, oldest=..., latest=..., limit=..., deadline=...)`.

**Edge Cases**

- 429/rate envelope → immediate `SlackRateLimitError(method, retry_after_seconds: int | None)`, using the shared M1 Retry-After resolver.
- Missing scope → sanitized reauthorization error before history.
- Repeated/malformed cursor, 100-page/10,000-candidate/deadline overflow → typed pagination/deadline error.
- Malformed envelope/message/timestamp → sanitized provider-response error.

**Tasks**

1. Implement exact request fields, scope mapping, and six-fraction timestamp conversion.
2. Implement envelope/page validation, bounded cursor paging, and rate-limit parsing.
3. Add adapter tests for all page sizes, bounds, scope combinations, terminal/empty pages, overlap, repeated cursors, safety ceilings, malformed data, deadline, and 429 behavior.

**Deliverable:** A provider seam returning a bounded validated result (`list[PrivateProviderMessage]`) or sanitized typed failure with no cache side effect. The result contains only validated provider messages; it contains no envelope, cursor, pagination metadata, or raw HTTP response.

**Verify:** `uv run pytest tests/test_slack_messages.py -k 'provider or pagination or scope or rate'`; inspect fake requests for canonical ID, exact fields, conversion, remaining limits, and immediate failure.

### M4.2 — Validation, authorization, normalization, ordering, and identity joins

**Dependency gate:** M1+M2+M3 MUST be complete. No public registration or duplicate cache/client seam is added here.

**Approach**

- Implement the public service around the shared policy and resolver. Validate before provider access, authorize before history, derive defaults from one clock read, and apply strict microsecond filtering locally.
- Bulk-join normalized users through the cache loader/refresh seam; normalize only the exact schema; deduplicate and sort deterministically.

**Wiring**

- **State:** immutable invocation bounds and limit; no durable message state.
- **Producers:** adapter candidates and normalized user snapshot.
- **Consumers:** local filter/dedup/order/projection; no consumer writes message content.
- **Call site:** `await messages('#general', after='2026-01-01T00:00:00Z', limit=20)`.

**Edge Cases**

- Neither bound → exact `(now-24h, now)`.
- One bound → only that exclusive bound.
- Boundary/subsecond/tie/duplicate message → precise exclusion and stable ordering.
- Unknown/bot/deleted author → retained with ID and nullable fields.
- Missing user cache → one complete refresh, never N+1 `users.info`.

**Tasks**

1. Implement signature, exact grammar, limit/default validation, and one-clock bounds.
2. Wire read policy, resolver, archive/deny ordering, canonical ID, and scope check.
3. Implement schema projection, safe field validation, bulk user join, filtering, canonical dedup, ordering, and permalink/reply metadata.
4. Add public tests for AC1–AC9, including cache read/refresh spies and no message writes.

**Deliverable:** `slack.messages` returns the exact safe newest-first list for one authorized conversation.

**Verify:** `uv run pytest tests/test_slack_messages.py`; assert exact schema/order, ISO and Slack precision, exclusive/default/one-sided bounds, limits, scope/rate errors, unknown authors, empty results, deny pre-call, and no N+1/cache writes.

### M4.3 — Replace the old Slack surface and document the final contract

**Dependency gate:** M1+M2+M3 and the central registry/allowlist and skill owner owned by M1 MUST be complete. This milestone may edit only the central owner’s existing registration/docs seams and this slice’s focused tests/docs; it MUST NOT create a local registry.

**Approach**

- Integrate `messages` into the central Slack registration owner without creating a second namespace or changing ownership of the other six functions. Remove `get_history` and every obsolete alias.
- Update the shared Slack skill/focused documentation with the exact final contract and no provider credentials/raw calls.

**Wiring**

- **Owner:** shared Slack foundation/`slack/__init__.py` owns the namespace and seven-function allowlist; this slice supplies one function entry and focused docs/tests only.
- **Registration:** `slack.messages` uses the established async exec-only metadata; no duplicate registration or compatibility wrapper.
- **Documentation:** signature, exact UTC grammar, bounds/defaults, schema/limits, scopes, authorization, cache seam, paging ceilings, and immediate rate-limit behavior.

**Edge Cases**

- Duplicate registration → fail rather than overwrite.
- Old function lookup/import → absent from the public registry.
- Documentation drift, missing scope, disabled policy, or missing credentials → focused regression failure or sanitized pre-provider error.

**Tasks**

1. Add the `messages` registration through the existing central owner and remove obsolete history names.
2. Add focused skill/documentation assertions for the final seven-function surface and exact contract.
3. Add registration/scope/documentation regression tests and run formatting/type checks.

**Deliverable:** The central namespace exposes `slack.messages` with no old history alias, no message cache, and no duplicate registration ownership.

**Verify:** Run exactly `uv run pytest tests/test_slack_messages.py tests/test_tool_registration.py`, then `uv run ruff check agent/src/archie_agent/exec/tools/slack tests/test_slack_messages.py` and `uv run mypy agent/src/archie_agent/exec/tools/slack`. Inspect the registered names and skill text to confirm the exact seven-function allowlist and absence of `get_history`; any missing repository command/tool is a milestone failure, not a reason to substitute an unrecorded command.