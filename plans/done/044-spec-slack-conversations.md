# 044 — Spec: `slack.conversations`

## Objective

Implement `slack.conversations` as the authenticated Slack user’s conversation directory. It MUST enumerate every accessible, non-archived conversation in which that user participates through Slack’s `users.conversations` API, enrich identities locally, and provide deterministic local type and query filtering without exposing provider payloads or denied conversations.

The result is a bounded, cache-backed discovery function for the personal-assistant Slack surface. It is not a workspace export, a channel-management API, or a compatibility layer for the old provider-shaped listing functions.

## Context

Project `042-project-slack-personal-assistant.md` defines this work as M3, dependent on the M1 foundation and M2 users, and locks the following decisions: the public surface is the replacement seven-function Slack interface; conversation discovery is user-centric; archived conversations are always excluded; muted conversations are never excluded; metadata is normalized and cached for 15 minutes; direct and broad read denylist enforcement is fail-closed; rate limits are surfaced immediately; and old public functions are removed without aliases.

The repository currently contains Slack OAuth provider registration but no current Slack conversation implementation, tests, or skill documentation to preserve. The implementation therefore follows the existing async service-tool architecture and the shared Slack seams established by the project and adjacent service plans, rather than adapting a legacy implementation. `slack.users` (spec 045) owns the normalized user directory and its 24-hour cache; this function consumes that directory for DM and group-DM identity joins. The shared Slack foundation (policy, credentials, errors, metadata-cache locking, and registration) and the users normalized snapshot MUST be established first; users/foundation work is a prerequisite, not an implementation detail of this spec. This function MUST NOT call `users.info` or create a second identity cache.

## Requirements

Each requirement is observable and includes acceptance criteria.

### Public API and validation

- The only public conversation function MUST be:

  ```python
  async def conversations(
      *,
      types: list[Literal["channel", "dm", "group_dm"]] | None = None,
      query: str | None = None,
      refresh: bool = False,
  ) -> list[dict]
  ```

- `types=None` MUST mean all three supported types. A supplied list MUST contain one or more distinct supported values; booleans, non-lists, empty strings, unknown values, and duplicate values MUST be rejected before provider access. Duplicate values MAY instead be normalized away only if the same validation rule is used consistently and documented; this spec chooses rejection.
- `query=None` MUST mean no query filter. A query MUST be a non-empty, whitespace-trimmed string of at most 200 Unicode characters. Matching MUST be case-insensitive using `casefold()` and MUST be substring matching, not fuzzy ranking.
- `refresh` MUST be a real boolean. `refresh=True` bypasses the conversation metadata cache and performs a complete refresh; it MUST NOT cause a partial cache to be used.
- Invalid arguments MUST raise the project’s sanitized Slack validation error and MUST make no Slack request.

### Exact result schema

Every returned item MUST be a JSON-serializable dictionary with exactly these keys (optional values are `None`, not omitted):

```python
{
    "id": str,                       # Slack conversation ID
    "type": Literal["channel", "dm", "group_dm"],
    "name": str | None,              # channel name; null for DMs when Slack has no name
    "is_private": bool | None,
    "is_muted": bool | None,
    "is_archived": bool,             # always False in a successful result
    "participants": [
        {
            "id": str,
            "username": str | None,
            "real_name": str | None,
            "display_name": str | None,
        },
    ],
}
```

- Channel `participants` MUST be an empty list. DM and group-DM participants MUST be ordered by the same identity sort key used for the enclosing conversation and MUST contain IDs even when identity enrichment fails. `users.conversations` does not provide a participant list for group DMs; therefore group-DM participant IDs MUST be obtained by a bounded `conversations.members` enrichment call for each normalized `mpim` record (never for channels or one-to-one DMs). The enrichment is part of the same complete refresh, not lazy result formatting.
- For a one-to-one DM, the exact peer field is the provider record’s `user` field: it MUST be a non-empty Slack user ID string, and `participants` MUST be `[user]`; the authenticated user MUST NOT be added. A missing, null, non-string, or self-valued `user` field is an unclassifiable/malformed DM and MUST fail the refresh (it MUST NOT trigger `conversations.members`). For a group DM, the provider record MUST have `is_mpim=true` (with `is_im` absent or false); `conversations.members` MUST return member objects or strings whose `id` values are non-empty Slack user IDs. The adapter MUST paginate that method to exhaustion, deduplicate IDs, and store the resulting list; an empty member list is valid, but a missing/malformed members field is not. The authenticated user is retained only if Slack explicitly returns that ID.
- `name` MUST be the provider channel name when present, otherwise `None`; the implementation MUST NOT invent names from IDs.
- Missing user identity fields are `None`. Unknown participant IDs remain visible as IDs because the conversation itself is not hidden solely by a missing user join.
- The function MUST NOT return raw Slack fields, topic/purpose, creator, timestamps, permissions, member counts, profile images, emails, or message content.
- Empty results MUST be `[]`, never `None`; ordering and filtering MUST be applied before return. The normalized snapshot MUST contain at most 20,000 conversations and each group-DM participant list at most 200 IDs; exceeding either bound is a sanitized bounded-result/provider-response error, never silent truncation. The public result MUST contain at most 500 items; if filtering still leaves more, raise a sanitized result-limit error rather than silently dropping items.

### Provider request and complete pagination

- The adapter MUST call Slack `users.conversations`, not `conversations.list`, `users.info`, or a legacy local listing function. Every refresh MUST enumerate all three types, regardless of the caller’s `types` filter; type filtering is local. This gives one cache coverage domain and guarantees that a later request for another type never receives a type-filtered snapshot.
- Every `users.conversations` request MUST include:
  - `exclude_archived=true` (boolean at the adapter boundary; encoded as the exact Slack form `true`);
  - `limit=200` (integer);
  - `types` encoded as one comma-separated string, with stable order `public_channel,private_channel,im,mpim` after expanding the requested types; and
  - `cursor` only after the first page, as the exact provider `response_metadata.next_cursor` string.
  The adapter MUST omit absent optional parameters rather than sending nulls. It MUST NOT send `exclude_muted`; muted conversations MUST remain in the result and normalized cache.
- The OAuth user token MUST have the read scopes required by the all-types request (`channels:read`, `groups:read`, `im:read`, and `mpim:read`); missing scope is a sanitized configuration/provider error and cannot be treated as an empty type. For every normalized `mpim` record, the adapter MUST call `conversations.members` with exactly `{"channel": <conversation id>, "limit": 200}` (encoded as integers/strings per the shared client boundary), following its `response_metadata.next_cursor` with the same 100-page and 60-second bounds. It MUST not send `conversations.members` for channels or DMs. This enrichment requires the authenticated token’s `mpim:read` scope (plus the existing identity scope); missing scope, denied access, malformed members, a member-page cap/deadline, or any transport/rate-limit failure fails the refresh and preserves the prior cache. `conversations.members` results MUST be cached only inside the refreshed conversation snapshot, keyed by conversation ID; a fresh snapshot hit makes zero provider calls. The adapter MUST deduplicate member IDs and retain the authenticated user only when Slack returns it.
- Pagination MUST be bounded by both a 100-page safety cap and a 60-second monotonic operation deadline. A non-terminating cursor, repeated cursor, malformed cursor metadata, cap hit, or deadline expiry MUST raise a sanitized `SlackPaginationError`. For every page, the provider envelope MUST be a JSON object with exactly `ok: true` plus the method list field (`channels` or `members`) and optional `response_metadata`; `response_metadata`, when present, MUST be an object whose `next_cursor` is either absent, the empty string, or a non-empty string of at most 4,000 characters. A missing list, null list, non-object envelope, non-boolean `ok`, `ok: false`, or malformed cursor metadata is an error. An empty `next_cursor` terminates pagination successfully; no request is made with an empty cursor. A non-empty cursor is passed byte-for-byte as the next request cursor. An empty page is valid whether its cursor is empty or non-empty; an empty page with a non-empty cursor continues pagination and is not treated as termination.
- A refresh is transactional: a complete successful enumeration replaces the prior complete conversation cache; any incomplete, malformed, transport, or rate-limit result MUST leave the prior complete cache untouched and MUST never be returned as a successful directory.

### Normalization, joins, filtering, and ordering

- Provider records MUST be normalized before persistence. Each success envelope MUST have `ok: true`, a list-valued `channels` for users.conversations or `members` for conversations.members, and an object-valued `response_metadata` when present; `ok` MUST be a real boolean and `error` MUST be absent on success; IDs and required type discriminators must be valid bounded strings, booleans must be actual booleans, and `next_cursor` must be absent/blank or a bounded non-repeated string. Reject malformed shapes, unknown fields are ignored only after validation, and never persist a provider object. Classify records exactly as follows: `is_channel=true` (and neither `is_im` nor `is_mpim` true) is `channel`; `is_im=true` (and `is_channel` and `is_mpim` not true) is `dm` and requires the `user` peer field; `is_mpim=true` (and `is_channel` and `is_im` not true) is `group_dm`; any conflicting, missing, or non-boolean discriminator is an error. Deduplicate by ID using deterministic first-page/first-seen record precedence: records are processed in page order and array order; the first record supplies the type and every field it provides, and later duplicates may fill only fields that were null/missing in the retained record. Later conflicting non-null values are ignored (the first valid value wins); participant IDs are the union, sorted case-sensitively by ID for the normalized snapshot, and any duplicate whose discriminator conflicts with the retained type is an error.
- Normalize Slack types to exactly `channel`, `dm`, or `group_dm`. Unsupported or unclassifiable records MUST raise a sanitized provider-response error rather than being guessed into a type.
- Archived records MUST be discarded defensively even though the request sets `exclude_archived=true`; `is_archived=True` MUST never be returned.
- The normalized conversation cache MUST retain exactly a versioned envelope plus normalized records: `id` (non-empty Slack ID), `type`, `name` (bounded string or null), `is_private` (boolean or null), `is_muted` (boolean or null), `is_archived` (boolean), and `participant_ids` (deduplicated non-empty ID strings). It MUST not retain messages or raw provider payloads. The cache namespace MUST be `slack.conversations`, partitioned by workspace/team ID, authenticated Slack user ID, and effective read-scope fingerprint. The scope fingerprint source is the sorted, case-sensitive tuple of OAuth scopes actually granted to the token, serialized as JSON with compact separators and hashed with SHA-256; it MUST include all granted scopes (not merely required scopes), so changed grants cannot reuse a snapshot. The path MUST be `${ARCHIE_HOME_DIR}/cache/slack/conversations/<team-id>/<user-id>/<scope-fingerprint>.json`; each path component MUST be encoded as lowercase RFC 4648 base32 of the UTF-8 bytes (padding removed), with `-` and `_` never used, and the filename is fixed. It MUST never be shared across teams, users, or scope sets. `fetched_at` is the UTC instant immediately after the complete provider enumeration and enrichment succeeds and immediately before serialization; `expires_at` is computed as `fetched_at + 900 seconds`; both MUST be UTC ISO-8601 timestamps with `Z` (for example `2025-01-02T03:04:05.678901Z`), parsed strictly. `complete` MUST be true only after users.conversations and all required members enrichment succeeds.
- Before formatting DM/group-DM results, perform one bulk join against the normalized user cache from `slack.users`. Do not issue one `users.info` call per participant. If the user cache is missing or expired, load it through the shared users-cache loader; if a participant is still absent, retain its ID with null identity fields.
- Policy evaluation MUST be ordered: validate arguments; obtain the immutable read-policy snapshot and reject disabled/malformed policy; obtain credentials and the granted-scope set needed to select the cache partition; load or refresh the complete conversation snapshot; perform the single bulk user join; resolve and validate every deny entry against the joined snapshot; remove denied conversations; apply the query; apply the requested type partition; sort; enforce the 500-item result cap; project. A valid cache hit skips only provider enumeration and member enrichment: policy validation, scope-fingerprint lookup, user-cache join, deny resolution, query, ordering, and result-cap checks still run. Flat deny resolution MUST be done once against the full joined snapshot before query/type filtering; any unresolved entry raises the sanitized policy-validation error, even if the caller’s query or type filter would exclude its potential target. The deny operation is a simple union of matching conversation IDs (no allow/deny precedence or ranking); each matching ID is removed, and resolution never expands a deny entry to related conversations.

- Apply the flat read denylist before returning any item. Deny matching MUST be case-insensitive and use these exact rules:
  - `#name` matches channel names (public or private), exact after casefolding;
  - `C...`, `G...`, or `D...` matches the conversation ID exactly after casefolding;
  - any other entry matches a one-to-one DM participant’s username, real name, display name, or email exactly after casefolding;
  - group DMs are never denied by participant lists; they require an ID entry;
  - unresolved configured entries fail closed with a sanitized policy-validation error, and denied names, IDs, participant identities, and counts MUST NOT be disclosed in model-visible output.
- Query matching MUST be local and case-insensitive over the conversation ID, channel `name`, and every returned participant `username`, `real_name`, and `display_name`. Email MAY be used for matching only if it is available in the internal normalized user cache, but MUST never appear in output. Denied items MUST be removed before query results are counted or returned.
- Deterministic ordering MUST be:
  1. channels: `(casefold(name or ""), id.casefold())`;
  2. DMs: `(casefold(primary participant display_name or real_name or username or id), id.casefold())`;
  3. group DMs: `(tuple(casefold(display_name or real_name or username or id) for participants), id.casefold())`.
  Return types in the caller’s requested type order, with the above ordering within each type. With `types=None`, use `channel`, then `dm`, then `group_dm`.

### Cache and refresh

- Persist only normalized conversation metadata in the shared Archie home cache under `ARCHIE_HOME_DIR`, using the existing restrictive-permission and concurrent-agent-safe cache conventions.
- A conversation cache record MUST contain a schema version, `fetched_at` timestamp, `complete=true`, and the normalized records. The TTL is exactly 15 minutes from `fetched_at`; missing, expired, corrupt, wrong-version, or `complete != true` records MUST be treated as cache misses.
- A cache hit MUST make no `users.conversations` request. Filtering, denylist evaluation, joins, and ordering remain local.
- `refresh=True` MUST bypass and replace the conversation cache only after complete success. There is no stale-cache fallback, including on rate limits.
- Cache read/write failures MUST be sanitized errors, not silently converted into a provider refresh or partial result. Atomic replacement MUST prevent readers from observing a partial write.

### Policy, rate limits, and replacement

- The authoritative Slack policy shape is exactly `{ "enabled": bool, "read": { "enabled": bool, "deny": list[str] }, "write": { "enabled": bool, "deny": list[str] } }`. M1 centrally owns this schema, defaults, validation, immutable snapshot, and shared enforcement; this spec only consumes the immutable read-policy snapshot. No alternate shape, aliases, missing keys, extra keys, or nested deny structures are permitted.
- Read policy MUST be checked before credentials and provider access. Disabled Slack reads return the shared sanitized policy error.
- HTTP 429 or a Slack rate-limit envelope from either provider method MUST immediately raise `SlackRateLimitError` containing only the exact attempted method (`users.conversations` or `conversations.members`) and `retry_after_seconds: int | None`. HTTP `Retry-After` takes precedence over an envelope value; only a valid non-negative integer is retained, while negative, non-finite, fractional, malformed, or absent values become `None`. No sleep, automatic retry, cursor continuation, stale-cache fallback, or cross-agent cooldown is permitted.
- Authentication, authorization, malformed-envelope, transport, deadline, pagination, cache, and policy failures MUST remain distinct sanitized error categories according to the shared Slack error mapping; tokens, URLs containing secrets, raw response bodies, and denylist details MUST not escape.
- Consume the M1-owned replacement registration: old channel/conversation listing functions and aliases are absent, and only the project’s seven redesigned Slack functions may be publicly registered. M1 centrally owns registration and removal.
- Consume the authoritative M1-owned Slack skill/documentation guidance; this spec contributes only the `slack.conversations` contract section. Do not document raw API calls or credentials.

### Acceptance criteria

- **AC1:** A valid call with a mocked multi-page all-types `users.conversations` response plus mocked group-DM member pages returns every unique non-archived conversation and makes exactly the required conversation/member calls until each cursor is empty.
- **AC2:** Recorded request arguments contain `exclude_archived=true`, integer `limit=200`, exact all-types `types=public_channel,private_channel,im,mpim`, never contain `exclude_muted`, and include cursors only on later pages; member requests contain only the documented channel and limit fields.
- **AC3:** A muted conversation is returned; an archived record injected into a response is not returned.
- **AC4:** DM and group-DM participant IDs are joined to normalized user identities in one bulk cache operation, with stable null fields for unknown users and no per-user provider calls.
- **AC5:** `types`, query, denylist, and deterministic ordering produce the exact documented output; query and denylist filtering are local and denied entries are not disclosed.
- **AC6:** A fresh cache hit makes zero provider calls; an expired cache and `refresh=True` each refresh; an incomplete refresh cannot replace or masquerade as a complete cache.
- **AC7:** Repeated cursors, 100-page overflow, deadline expiry, malformed pages, and rate limits fail with the specified sanitized error and do not serve stale data.
- **AC8:** Old public listing functions are absent, and the skill/docs/tests describe and exercise only the replacement function.
- **AC9:** A cache hit is valid only for the matching namespace/team/user/scope partition, `coverage="all"`, strict UTC `fetched_at`/`expires_at`, complete marker, and 900-second TTL; type-filtered calls make zero provider calls and filter locally. Cache tests cover stale, corrupt, wrong partition, wrong coverage, atomic replacement, and failed refresh preservation.
- **AC10:** Group-DM enrichment tests prove exact `conversations.members` payloads, pagination, `mpim:read` scope handling, member caps, unknown IDs/null joins, and failure without partial publication. Result-limit and snapshot-limit tests prove errors rather than truncation; response-shape tests cover malformed envelopes, fields, IDs, booleans, and cursors.
- **AC11:** Policy tests prove unresolved deny entries fail closed without disclosing entries, identities, or counts; registration/docs tests verify the seven-function registry, current skill path, exact schema, scopes, cache partitioning, request encoding, bounds, and all verification commands.

## Technical Design

### Overview

Implement a thin Slack adapter plus a normalized conversation projection and cache-backed service function. The adapter owns Slack request construction, envelope validation, cursor pagination, deadline enforcement, and rate-limit mapping. The service owns cache selection/replacement, user-cache joins, policy/denylist filtering, query matching, ordering, and the exact JSON result projection.

Use the shared Slack operation context: immutable policy snapshot, credential/client, monotonic deadline, normalized metadata caches, and provider error mapping. Conversation content is never cached. This design deliberately uses `users.conversations` rather than broad channel enumeration because membership is the product boundary.

### Architecture and components

- `agent/src/archie_agent/exec/tools/slack/conversations.py`: public async `conversations` function, validation, cache orchestration, local filtering/order, and projection.
- `agent/src/archie_agent/exec/tools/slack/client.py`: shared async Slack Web API boundary; add `users.conversations` and `conversations.members`, common envelope/error validation, cursor handling, exact request encoding, scope checks, and sanitized rate-limit mapping.
- `agent/src/archie_agent/exec/tools/slack/models.py`: normalized `ConversationRecord`, `ParticipantRef`, cache record, and result typing/projection helpers. Keep provider payload types separate.
- `agent/src/archie_agent/exec/tools/slack/cache.py`: reuse or extend the metadata cache abstraction used by users; atomic writes, restrictive permissions, schema/version/completeness checks, and TTL evaluation.
- `agent/src/archie_agent/exec/tools/slack/policy.py`: reuse shared read policy and flat Slack denylist resolution; conversation-specific matching belongs here or in a clearly named policy helper, not in the provider client.
- `agent/src/archie_agent/exec/tools/slack/__init__.py` and the existing M1-owned registration seam: M1 centrally registers the replacement public function with the project’s service-tool metadata (`exec_enabled=True`, `exec_docs=False`, `native=False`, `namespace="slack"`) while preserving unrelated tools; this spec supplies only the callable contract.
- `persona/skills/slack/SKILL.md`: M1 centrally owns the shared seven-function skill guidance; this spec contributes only the `slack.conversations` contract section and must not replace, fork, or relocate the skill.
- `tests/test_slack_conversations.py` and shared Slack registration/cache tests: test the public function through mocked provider and cache seams, not private implementation details.

### Data model and lifecycle

`ConversationRecord` has required `id: str`, `type: ConversationType`, `name: str | None`, `is_private: bool | None`, `is_muted: bool | None`, `is_archived: bool`, and `participant_ids: tuple[str, ...]`. It is normalized, in-memory/cache-safe metadata; no message content is present. `ConversationCacheRecord` has required `schema_version`, `team_id`, `user_id`, `scope_fingerprint`, `fetched_at`, `expires_at`, `coverage="all"`, `complete`, and `conversations`.

Use the shared user normalized record for joins; do not duplicate or persist full user profiles here. A cache is usable only when its schema version, partition keys, `coverage`, completeness, strict UTC timestamps, and 900-second TTL validate. A successful refresh writes a newly serialized complete record atomically; failures do not mutate the old record. Both the snapshot and public result have explicit caps (20,000 and 500 respectively); exceeding them is an error, not truncation.

### Data flow

1. Validate arguments and resolve the immutable read policy before credentials/provider access.
2. Read the `slack.conversations` cache unless `refresh=True`; reject invalid, expired, wrong-team/user/scope, or non-all-types records as misses.
3. On a miss, call all-types `users.conversations` with the fixed parameters, follow and validate every page within the 100-page/60-second bounds, normalize/deduplicate records, then call bounded `conversations.members` for each group DM. Stage all data and atomically commit only the complete result.
4. Ensure the shared normalized user cache is available, then join participant IDs in memory. Do not make `users.info` calls or user calls from the conversation loop.
5. Resolve/validate the flat denylist, remove denied records, apply the local query, partition by requested type, sort with the exact keys, and project the exact result dictionaries.
6. Return `[]` or the bounded normalized list. Provider errors stop this flow immediately; no stale or partial directory is returned.

### External integration and error behavior

Slack is called through the existing OAuth user token and async HTTP client conventions. The adapter uses the shared request deadline and timeout configuration; it performs no retries. A Slack `ok: false` rate-limit response and HTTP 429 map to `SlackRateLimitError`; malformed success envelopes map to a provider-response error; cursor non-termination maps to `SlackPaginationError`; transport/deadline failures map distinctly. All errors are sanitized before reaching the model.

### Non-functional constraints

- Security: no token, raw provider response, email, denied identity, or message content enters the result or cache; cache files remain under Archie home with restrictive permissions and atomic replacement.
- Reliability: complete-or-error enumeration is mandatory; a partial page set can never be marked complete or replace a complete cache.
- Performance: one provider pagination pass per cache refresh and one bulk user-cache join; no N+1 identity requests. Local filtering/order must not call Slack.
- Observability: tests and structured debug logging MAY record page count and cache hit/miss internally, but model-visible errors MUST omit hidden counts and policy details.

### Key decisions

- `users.conversations` over `conversations.list`: it enforces authenticated-user participation at the provider boundary; broad enumeration was rejected because it creates membership and denylist ambiguity.
- No `exclude_muted`: muted conversations are part of the user’s directory and the project explicitly requires them; sending the parameter would silently lose user context.
- Complete refresh over stale fallback: a partial directory is more misleading than an explicit failure, and the project forbids stale metadata fallback.
- Local filtering/order over Slack ordering: Slack does not provide the user-facing deterministic ordering or query semantics required by the contract.

## Milestones

### M3.1 — Complete conversation and group-DM provider seams

**Dependency gate:** This milestone MUST begin only after M1’s shared Slack foundation and M2/Spec 045’s normalized `slack.users` cache contract are implemented and verified. It does not establish users or the shared foundation; the conversation implementation consumes those seams and MUST NOT duplicate them.

**Approach**

- Extend the shared async Slack client, following the existing provider/error conventions, with the exact all-types request mapping, response normalization, and bounded `conversations.members` enrichment described above. Group-DM enrichment is mandatory because `users.conversations` supplies no participant list; a scope/access failure is a refresh failure, not an empty participant list.
- Use a monotonic 60-second deadline and a 100-page safety cap; validate `next_cursor` before each continuation and stop immediately on any error.
- The test seam is the public adapter boundary with a fake HTTP client: assertions inspect every request and the mapped error, not HTTP library internals.
- ⚠️ Never send `exclude_muted`; never treat a page that stops due to a cap/deadline as complete.

**Wiring**

- **State:** per-invocation pagination state (`seen_cursors`, page count, deadline, normalized records), owned by the provider adapter.
- **Producers:** each successful Slack page contributes records and one next cursor; no producer may commit cache state.
- **Consumers:** the conversation service consumes only the adapter’s complete normalized result or a typed error.
- **Call site:** `await slack_client.users_conversations(types="public_channel,private_channel,im,mpim", deadline=deadline)`, followed for each `mpim` by `await slack_client.conversations_members(channel=conversation_id, deadline=deadline)`.

**Edge Cases**

- HTTP 429 or Slack rate-limit envelope → raise `SlackRateLimitError` immediately; do not continue.
- Repeated/non-string cursor → raise `SlackPaginationError`.
- 100th-page continuation or deadline expiry → raise `SlackPaginationError`; result is incomplete.
- Duplicate ID or missing optional field → normalize/deduplicate; missing required ID/type or malformed envelope → provider-response error.

**Tasks**

- Implement the `users.conversations` request and exact type mapping.
- Implement envelope validation, cursor pagination, page/deadline bounds, and error mapping.
- Add adapter tests for exact all-types request parameters, group-DM member enrichment requests/scopes/pagination, multi-page completion, repeated cursors, cap/deadline, malformed responses, result/snapshot caps, and rate limits.

**Deliverable**

The Slack adapter returns a complete normalized conversation set or a typed failure, while issuing no muted-exclusion parameter.

**Verify**

Run `uv run pytest tests/test_slack_conversations.py -k 'provider or pagination or members or rate'` and inspect mocked request records for `exclude_archived=True`, integer limits, exact all-types encoding, absence of `exclude_muted`, exact member payloads/scopes, correct cursors, and immediate failure cases.

### M3.2 — Normalized cache, user joins, policy filtering, and exact output

**Dependency gate:** Requires M3.1’s complete normalized provider adapter plus the M1 foundation and M2 users seams.

**Approach**

- Follow the normalized users cache and shared atomic metadata-cache pattern; store only the schema-versioned complete all-types conversation metadata record with a 900-second TTL. Partition storage by namespace/team/user/scope fingerprint and use strict UTC `Z` timestamps; never use a type-filtered snapshot.
- Put all query matching, denylist resolution, participant joins, partitioning, and ordering after cache/provider normalization and before projection. The public-function test seam is `await conversations(...)` with fake cache, user directory, policy, and Slack seams.
- Use one bulk user-cache lookup; unknown IDs remain as null-field participants. Apply denylist filtering before query matching and before result counts.

**Wiring**

- **State:** `ConversationCacheRecord` is created by the refresh path and atomically persisted under `ARCHIE_HOME_DIR`; the user cache is read from the shared users-cache owner.
- **Producers:** a complete adapter result produces the new conversation cache; failed refreshes produce no write.
- **Consumers:** every invocation reads cache state, joins the user directory, then applies the immutable policy snapshot and local filters.
- **Call site:** `await conversations(types=["dm"], query="alice", refresh=False)` returns exact result dictionaries, never provider records.

**Edge Cases**

- Missing/expired/corrupt/incomplete cache → full refresh; no stale fallback.
- `refresh=True` with provider failure → raise the provider error and preserve the prior complete cache.
- Missing participant identity → retain participant ID and null identity fields.
- Archived or duplicate provider item → discard archived item and merge/deduplicate by ID.
- Denylist entry cannot resolve → fail closed with sanitized policy error; do not disclose the entry or hidden counts.
- Query matches only a denied item → return `[]`.

**Tasks**

- Add normalized conversation/cache models and atomic TTL-aware persistence.
- Implement cache hit/miss/refresh orchestration and transactional replacement.
- Implement bulk participant identity joins and the exact result projection.
- Implement type validation, local query matching, flat denylist filtering, and deterministic ordering.
- Add public tests for muted/archived records, DM/group-DM joins, unknown identities, cache TTL/refresh/failure, filtering/order, and denylist behavior.

**Deliverable**

`slack.conversations` returns the exact documented, joined, policy-filtered directory from a complete 15-minute cache or a complete refresh.

**Verify**

Run `uv run pytest tests/test_slack_conversations.py`; tests must assert exact list equality, zero provider calls on cache hits, no cache replacement on failed refresh, one bulk user lookup, muted inclusion, archived exclusion, deny filtering, and all three ordering keys.

### M3.3 — Integrate through the M1 central registry and document the contract

**Dependency gate:** Requires M3.1–M3.2 plus the M1 central registry/allowlist seam. This milestone owns only the conversation import, obsolete-listing removal, and focused contract tests/docs; M1 centrally owns the namespace, seven-function allowlist, registration rules, and shared skill guidance.

**Approach**

- Do not create a local Slack registry or register a second namespace. M1 centrally owns the registry import, obsolete-listing removal, exact-seven allowlist/metadata entry, duplicate detection, and registration rules. This spec provides the `slack.conversations` callable and contract assertions through the existing M1 seam; the old import/name MUST be absent and unrelated registry entries MUST be preserved.
- Contribute the exact signature/result schema and focused contract text to the M1-owned Slack skill guidance, covering safe sequencing, cache refresh, local filters, muted behavior, limits, and errors. The test seam is the central registration and skill discovery/content seam.
- Do not add provider credentials to arguments, permanent raw API documentation, or a second Slack cache.

**Wiring**

**Edge Cases**

- Duplicate public registration → fail during registration without overwriting an existing function.
- Old function import or name → absent from the public registry; no compatibility alias.
- Missing Slack credentials/read policy → sanitized configuration/policy error before provider access.

**Tasks**

- Provide the conversation callable and contract metadata to the M1-owned central registry seam; M1 performs the central import, obsolete Slack conversation/channel listing removal, exact-seven allowlist/metadata update, and registration. No second Slack namespace or local registry is permitted.
- Remove obsolete Slack conversation/channel listing references in the focused conversation implementation/tests; M1 owns their central registration removal.
- Contribute this contract to the M1-owned `persona/skills/slack/SKILL.md`; do not create, replace, fork, or relocate the shared skill.
- Add focused conversation contract and regression tests against the M1-owned registration and skill seams; run formatting/type checks used by the repository.

**Deliverable**

The replacement Slack namespace exposes the documented `conversations` function and no old conversation-listing alias.

**Verify**

Run `uv run pytest tests/test_slack_conversations.py tests/test_tool_registration.py` (or the repository’s equivalent registration test path), then inspect the registered Slack names and skill text to confirm the seven-function surface, exact signature, and absence of old names.
