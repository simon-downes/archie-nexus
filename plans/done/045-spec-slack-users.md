# 045 — Spec: Slack users

## Objective

Implement the M2 `slack.users` contract as a safe, local user directory for the authenticated Slack user. It depends on M1 and owns only complete `users.list` enumeration, the normalized users cache, and the deterministic context-bound identity lookup seam; it does not wire conversations, messages, search, threads, sending, reactions, formatters, registration, or other downstream consumers. Those consumers belong to M3–M7.

## Context

The Slack personal-assistant project uses `slack.users` as the source of normalized identities. Slack returns provider-shaped profiles, paginates the directory, conditionally returns email, and mixes human, bot, app, and deleted records. The existing OAuth/client, immutable session-policy, shared metadata-cache, and sanitized-error contracts in `042-project-slack-personal-assistant.md` remain authoritative. Credentials remain in the typed runtime credential path and are never arguments, cache values, results, logs, or documentation examples.

`users.list` is the only provider operation in scope. A successful refresh replaces the complete user snapshot atomically. A partial, malformed, rate-limited, unauthorized, or otherwise failed refresh never replaces a previous snapshot and is never presented as a complete directory. `refresh=True` requests current data; it never permits stale fallback.

## RFC2119 Requirements and acceptance criteria

### Public contract and result schema

1. `slack.users` MUST expose exactly this public async signature:

   ```python
   async def users(
       query: str | None = None,
       *,
       refresh: bool = False,
       include_deleted: bool = False,
   ) -> list[dict]:
   ```

   `query=None`, `query=""`, and a whitespace-only query mean an empty query. Surrounding whitespace MUST be removed. `query` MUST be a string or `None` and MUST be at most 200 Unicode characters before trimming. `refresh` and `include_deleted` MUST be actual booleans, not truthy coercions. Invalid arguments MUST fail before policy credential/provider use.

2. Every returned record MUST contain exactly these seven keys:

   ```python
   {
       "id": str,
       "username": str | None,
       "real_name": str | None,
       "display_name": str | None,
       "email": str | None,
       "deleted": bool,
       "is_bot": bool,
   }
   ```

   IDs MUST be non-empty strings of at most 128 characters. Text fields MUST be Unicode-normalized, whitespace-collapsed, and at most 256 Unicode characters; empty values become `None`. Email is lower-cased only by the email normalizer and is `None` unless returned by Slack while `users:read.email` is authorized. No `profile`, image URL, phone, title, status, timezone, presence, `bot_id`, `is_app_user`, raw provider field, or arbitrary future field may appear in the result or cache.

3. The normalized provider truth table MUST be:

   | `deleted` | `is_bot` source flags (`is_bot`, `is_app_user`, `bot_id`) | normalized `deleted` | normalized `is_bot` | ordinary output |
   |---|---|---:|---:|---|
   | absent/false | all absent/false | false | false | included |
   | true | all absent/false | true | false | excluded unless `include_deleted=True` |
   | absent/false | any true/non-empty | false | true | always excluded |
   | true | any true/non-empty | true | true | always excluded |

   Boolean source flags, when present, MUST be actual booleans; `bot_id` MUST be a bounded string when present. `is_bot` is true if any bot/app signal is true/non-empty. This is the sole normalized bot rule; missing signals mean false. An unclassifiable flag is a malformed response.

4. A provider member without a valid ID is malformed. Duplicate members are grouped by ID after normalization. Status conflicts (`deleted` or `is_bot`) MUST raise a sanitized malformed-response error. For each nullable text field, identical values are accepted; if values differ, the deterministic merge is: choose the non-`None` value over `None`, otherwise choose the lexicographically smallest value by Unicode code point. Thus duplicate order never affects the result. The same rule applies to email. No raw duplicate is persisted. Every final ID occurs once. Duplicate records are not last-write-wins, and no provider order or timestamp breaks a conflict.

5. Empty-query output MUST contain active, non-bot users by default. `include_deleted=True` adds deleted, non-bot users and changes no bot behavior. The internal snapshot retains every valid normalized record, including bots and deleted users.

6. Non-empty queries MUST use this exact local matching algorithm. For names, usernames, and queries, normalization is `unicodedata.normalize("NFKC", value)`, replace each maximal run matching Unicode `\s` with one ASCII space, then `strip()` and `casefold()`; length bounds are checked on the pre-normalized provider value/query. Email normalization MUST first apply the same NFKC/Unicode-whitespace collapse/trim, then require one `@`, a non-empty local and domain part, no whitespace, and total length at most 256, and finally apply `casefold()` to the entire address (invalid email becomes `None` and is a malformed member when supplied). Search fields, in rank order, are `display_name`, `real_name`, `username`, `email`; absent fields are omitted. A record matches when the normalized query is (a) equal to a whole field, (b) a substring of a whole field, or (c) every query token, obtained by splitting on ASCII spaces, is a substring of at least one token in that field. Empty tokens are impossible. The best match across fields determines the score: exact whole-field `(0, field_rank, 0)`, whole-field substring `(1, field_rank, character_start)`, and all-token match `(2, field_rank, token_start)`; lower tuples rank first. Email participates only when present and authorized. No edit distance, stemming, punctuation removal, provider ordering, or remote lookup is permitted.

7. Results MUST be ordered by match score for non-empty queries, then by this stable identity key: `(casefold(display_name or ""), casefold(real_name or ""), casefold(username or ""), id.casefold(), id)`. Empty-query results use only that stable identity key. Provider page/order MUST never affect output.

8. The public result MUST contain at most 500 records and serialize to at most 262,144 UTF-8 JSON bytes using compact JSON. If filtering produces more than 500 records or serialization exceeds 262,144 bytes, the function MUST raise a sanitized bounded-result error; it MUST NOT truncate. The cache may contain up to 10,000 normalized users, but a complete cache that cannot satisfy these public bounds is not a successful result.

### Complete enumeration, OAuth, cache, and refresh

9. The adapter MUST call `users.list` with `limit=200`, no exposed cursor argument, and the authenticated user token. It MUST follow `response_metadata.next_cursor` until absent or blank. Enumeration is bounded by exactly 100 pages, 10,000 unique normalized users, and a 60-second monotonic invocation deadline. Repeated cursors, invalid metadata, page-cap exhaustion, user-cap exhaustion, or deadline expiry MUST raise sanitized pagination/incomplete errors.

10. Each response MUST have `ok: true`, `members` as a list, and object-valued `response_metadata` when present. `next_cursor` MUST be absent, blank, or a string of at most 256 characters. A page containing zero members with a non-blank cursor is malformed/incomplete and MUST fail. Unknown fields may be ignored only after shape validation. A successful zero-member directory is valid.

11. The existing shared Slack OAuth configuration owns one explicit boolean `request_email_scope` setting (default `false`) and the existing shared credential validator owns scope validation and reauthorization. Every users session requests and requires `users:read`; when `request_email_scope=true`, the shared OAuth request MUST additionally request `users:read.email`, and the validator MUST reject an existing token that lacks that grant before `users.list`, with a sanitized error naming only `users:read.email` and requiring reauthorization. It MUST NOT silently downgrade the configured request. When `request_email_scope=false`, an existing token with or without `users:read.email` is accepted; the validator reports the actual grant, and absent email scope is the ordinary no-email state. The validator MUST return the immutable tuple `(team_id, user_id, granted_scopes)` from the existing token/session credential context; this tuple is the sole source of identity and scope state, and no token is passed to or stored by this slice. Scope validation occurs before provider access.

12. The cache identity MUST include all of `team_id`, authenticated Slack `user_id`, and `email_scope_state`, where `email_scope_state` is exactly `email-authorized` iff `users:read.email` is in the validator's returned `granted_scopes`, otherwise `email-unavailable`. These values MUST come only from the validated session tuple in requirement 11; they MUST NOT be inferred from a response or caller input. The cache namespace MUST be `slack.users`, with path-safe percent encoding UTF-8 bytes (uppercase `%HH`; unreserved ASCII `A-Z`, `a-z`, `0-9`, `.`, `_`, `-`, `~` unchanged) applied separately to `team_id`, `user_id`, and `email_scope_state`: `slack.users/{quote(team_id,safe='.-_~')}/{quote(user_id,safe='.-_~')}/{email_scope_state}`. Reject empty, `.` or `..` identity components and encoded separators before path construction. No snapshot may be read across teams, authenticated users, or email-scope states; the partition key and cache record MUST never contain a token.

13. A cache miss, expired cache, corrupt/schema-incompatible cache, incomplete cache, identity mismatch, or `refresh=True` MUST cause a complete refresh. A refresh stages all pages in memory, normalizes and validates the whole directory, then writes one complete snapshot. Failed refreshes leave the previous complete snapshot untouched and return the failure, including when a fresh snapshot exists. No retry and no stale fallback are permitted.

14. The cache envelope MUST contain exactly the versioned metadata needed by this spec plus safe users: `schema_version`, `team_id`, `user_id`, `email_scope_state`, `fetched_at`, `expires_at`, `complete`, and `users`. The UTC clock is injected and sampled exactly once, immediately after the final page has been validated and before publication; that instant is `fetched_at`, serialized only as timezone-aware UTC RFC3339 with `Z` (seconds precision, no fractional seconds). `expires_at` is persisted as exactly `fetched_at + 24 hours`, using the same format. Freshness is `now_utc < expires_at`; equality is expired. TTL is not extended by reads or local filtering. The 60-second operation deadline MUST use a separate injected monotonic clock. `complete` MUST be true only after all validation succeeds; publication writes the complete envelope once, never an intermediate timestamp or partial users list.

15. Cache reads and writes MUST use the shared Archie-home metadata-cache owner and restrictive permissions. The partition lock MUST be a cross-process advisory file lock on the partition (not an in-process mutex), held through cache recheck, provider enumeration, validation, and atomic publication. A waiter MUST re-read the partition after acquiring the lock and use the newly published fresh snapshot unless its call has `refresh=True`; refresh callers serialize and each performs its requested refresh. Writes MUST use a same-directory temporary file, compact JSON, flush and `fsync`, atomic replace, and directory `fsync` where supported. Corruption, permission failure, and lock failure are sanitized errors or cache misses as appropriate. A lock MUST never convert a provider failure into stale success.

16. The cache MUST contain only the seven normalized fields and the envelope fields above. Raw Slack responses, profiles, credentials, tokens, scope payloads, and provider error text MUST never be written.

### Policy and identity lookup seam

17. The shared Slack policy owner MUST own parsing and validation of the authoritative configuration shape exactly as `{ "enabled": bool, "read": { "enabled": bool, "deny": list[str] }, "write": { "enabled": bool, "deny": list[str] } }`, with defaults `{ "enabled": true, "read": { "enabled": true, "deny": [] }, "write": { "enabled": false, "deny": [] } }`. Immutable session snapshots and read enablement are also shared-owner responsibilities. This spec MUST NOT parse, mutate, persist, or redefine policy. The shared owner MUST expose `SlackReadPolicySnapshot(enabled: bool, deny: tuple[str, ...])`; `deny` MUST be a flat list of strings, with no nested rules, aliases, wildcards, or fuzzy expressions. Read-disabled Slack access MUST fail before credentials/provider access. This spec consumes only that snapshot and owns user-side resolution of its entries.

18. User deny entries MUST resolve exactly using the identity-alias normalizer: NFKC, replace each Unicode `\s` run with one ASCII space, trim, then casefold. Entries beginning with `#`, or matching `C...`, `G...`, or `D...`, are conversation entries and MUST be rejected by the user resolver rather than treated as user substrings. Other entries match only exact `id` (when a valid Slack user ID is supplied), `username`, `real_name`, `display_name`, or authorized `email`. A missing match or an alias matching multiple IDs MUST fail closed with a generic sanitized policy-resolution error. Partial names, prefixes, fuzzy query matches, and guessed aliases MUST never resolve. Denied IDs MUST be removed before matching, ordering, counting, and serialization, with no disclosure of their existence.

19. This spec MUST expose exactly this read-only, context-bound identity seam: `async def lookup_user_ids(*, session: UsersSessionContext, ids: Collection[str]) -> Mapping[str, NormalizedUser | None]`. `UsersSessionContext` is the already validated immutable tuple `(team_id, user_id, email_scope_state, read_policy_snapshot)` from the same session; callers cannot supply a cache path, token, scope state, or policy separately. `ids` MUST contain at most 500 entries, each a unique non-empty string of at most `MAX_ID_LENGTH`; invalid type, duplicate, or bound violations fail before cache/provider access. The seam reads only the matching complete safe snapshot, performs no provider call, returns one entry per requested ID (including `None` for absent or policy-denied IDs), and returns only the seven normalized fields. It MUST never return bots or deleted users, and MUST apply the supplied session policy without resolving aliases. It MUST NOT wire or modify conversations, messages, search, or thread features; no downstream registration, formatter, or API call belongs in this spec.

### Errors, bounds, and rate limits

20. Named implementation constants MUST be exactly: `USERS_PAGE_SIZE=200`, `MAX_USERS_PAGES=100`, `MAX_NORMALIZED_USERS=10_000`, `MAX_ID_LENGTH=128`, `MAX_FIELD_LENGTH=256`, `MAX_CURSOR_LENGTH=256`, `MAX_QUERY_LENGTH=200`, `MAX_PUBLIC_USERS=500`, `MAX_SERIALIZED_RESULT_BYTES=262_144`, `USERS_TTL=24h`, and `USERS_DEADLINE=60s`. Tests MUST assert each bound and its failure behavior.

21. Provider errors MUST map to sanitized shared Slack categories. A rate-limit response MUST fail immediately with method `users.list` and `retry_after_seconds: int | None`; only a valid non-negative integer number of seconds is retained, while missing or invalid values become `None`. It MUST not sleep, retry, continue pagination, or use stale cache. Authentication/configuration/scope, malformed response, transport/deadline, pagination, cache, policy, and bounded-result errors MUST not contain tokens, raw payloads, profiles, aliases, IDs of hidden users, or stack traces.

22. No `users.info`, automatic retry, raw profile persistence, cursor exposure, credential exposure, or partial-directory success is permitted.

## Technical design

### Normalized model and cache record

The internal `NormalizedUser` model has exactly the seven public fields and no raw-payload reference. Provider mapping occurs at the adapter boundary. The cache envelope is JSON-safe, schema-versioned, partition-keyed, complete-only, and contains only the fields in requirement 14. A schema or partition mismatch is a cache miss, never a guessed migration.

### Provider pagination and response mapping

The injected provider seam is `users_list(*, cursor: str | None, limit: int, deadline: float) -> ProviderPage`; the public function never exposes these arguments. The fake and real adapter both enforce `limit=200`. Every page is validated and normalized before being staged. Duplicate merging uses the ID/status/text rule in requirement 4. The refresh builder publishes only after cursor termination and all bounds pass.

### Query/index and ordering

The per-invocation index is built only from the safe snapshot. It stores normalized field values and field rank, applies status and deny filtering before scoring, and uses the exact three match classes and stable key above. Empty query is a directory path, not a substring match.

### Policy and identity ownership

M1’s shared policy layer owns configuration parsing, immutable session snapshots, and read enablement. M2 owns exact user alias resolution against a complete normalized snapshot and exposes the ID lookup seam. It does not resolve conversations or perform downstream wiring; downstream consumer wiring belongs to M3–M7.

### Files, registration, and documentation

Implementation MUST be limited to the M2 Slack users implementation, its M1-owned shared cache/policy/credential seams, and focused user documentation. M1 owns central Slack namespace registration and the OAuth credential path; this spec MUST NOT create a second registration, credential store, or downstream consumer wiring. Expected focused tests are `tests/test_slack_users.py` and any existing shared seam tests required by the implementation. Tests MUST be offline and use injected provider, cache storage, UTC/monotonic clocks, cross-process lock, credential/scope state, policy snapshot, and serializer seams. User documentation may describe this contract but MUST contain no credentials or raw-provider examples.

## Milestones

### M2.1

#### Approach

Consume M1’s exact seven-field identity, bot/app/deleted truth table, strict argument, OAuth scope, numeric-constant, error, and operation seams before implementing enumeration. M2 does not own registration or downstream wiring.

#### Wiring

M2 consumes M1’s validated session, policy, credential, and error seams; enumerates and caches the complete normalized user snapshot; and exposes only the context-bound `lookup_user_ids` seam for downstream consumers. M2 does not wire downstream features or own central registration.

#### Edge Cases

- wrong argument types and read-disabled policy fail before provider access;
- missing optional email scope yields `email-unavailable`, not a name-lookup failure;
- configured email scope without a grant requires reauthorization;
- bots, app users, deleted users, and conflicting duplicate status flags follow the truth table;
- nullable fields, redaction, query length, and result serialization bounds are enforced.

#### Tasks

1. Define the normalized model, truth table, public projection, constants, sanitized error types, exact query normalization, and serializer bound.
2. Add injected provider, cache, credential/scope, policy, UTC/monotonic clock, lock, and identity-lookup seams.
3. Consume the M1-owned registration and credential seams without changing registration, adding aliases, or adding downstream integrations.
4. Add offline tests for validation, scope states, projection redaction, truth-table status filtering, matching primitives, and bounds.

#### Deliverable

Focused contract tests pass and `slack.users` returns exactly the seven-field normalized projection using the validated email-scope state, with no raw provider field.

#### Verify

Run `uv run pytest tests/test_slack_users.py -q` and `uv run ruff check <changed Slack Python files>`. Assert exact keys, no raw fields/credentials, all named constants, exact scope/reauthorization behavior, and pre-provider validation.

### M2.2

#### Approach

Implement the bounded cursor adapter and staged refresh using the shared Archie-home cache owner. Publish only a complete validated snapshot under the team/user/email-scope partition.

#### Wiring

On miss, expiry, identity mismatch, or refresh, the cross-process partition lock rechecks the cache, enumerates pages with the injected deadline, validates and merges records, then atomically publishes the snapshot. Ordinary calls read only a fresh complete matching partition.

#### Edge Cases

- multi-page success, zero-member success, cursor termination, repeated/invalid cursor, 100-page cap, 10,000-user cap, and 60-second deadline;
- malformed envelopes, members, flags, IDs, metadata, and mid-refresh failures;
- duplicate IDs are order-independent and status conflicts fail;
- failed refresh preserves but never returns stale data;
- UTC expiry is exactly 24 hours and is not extended by reads;
- separate teams/users/email-scope states never share cache data;
- concurrent processes cannot publish interleaved or partial data;
- 429 returns immediately with no stale fallback.

#### Tasks

1. Implement `users.list` request mapping with `limit=200` and exact pagination bounds.
2. Implement response validation, normalization, truth-table mapping, deterministic duplicate merging, and complete-cursor detection.
3. Implement versioned partitioned cache envelopes, UTC timestamps, atomic restricted writes, and cross-process locking.
4. Add injected provider, storage, clock, deadline, and lock tests, including a subprocess lock test where supported.
5. Confirm no `users.info` path exists.

#### Deliverable

A focused cache test records one complete, atomically published normalized snapshot in the exact team/user/email-scope partition and rejects incomplete refreshes.

#### Verify

Run `uv run pytest tests/test_slack_users.py -q -k 'pagination or cache or lock or scope'`; run `uv run ruff check <changed Slack Python files>`. Assert exact request cursors/call counts, no partial writes, atomic publication, cache isolation, expiry/refresh behavior, and no raw response persistence.

### M2.3

#### Approach

Build the exact local index and exact deny resolver over normalized data. Keep query fuzzy matching separate from policy identity matching and expose only the bounded ID lookup seam.

#### Wiring

After snapshot load, resolve the immutable flat read denylist in one pass, remove denied IDs and ineligible statuses, apply the specified three-class matcher and score, then apply the stable identity order and serialization bound. M2 owns no conversations, messages, search, or thread wiring; those downstream consumers belong to M3–M7.

#### Edge Cases

- empty versus whitespace query;
- NFKC/casefold/whitespace normalization, whole-field, substring, and all-token matching;
- each field rank, email authorized/unavailable behavior, Unicode ties, duplicate page permutations;
- exact ID/alias deny matches, conversation-entry rejection, unknown/ambiguous fail-closed resolution;
- hidden identities never affect visible counts, result limits, or errors;
- `lookup_user_ids` returns safe status-bearing records or `None` without provider access.

#### Tasks

1. Implement the exact matcher, field ranks, match scores, stable ordering, and result serializer bound.
2. Implement flat denylist user resolution and generic fail-closed errors.
3. Implement and test `lookup_user_ids` with bounded IDs and no provider path.
5. Add focused user documentation for this contract.

#### Deliverable

Focused lookup tests return the deterministic filtered list and the bounded context-bound `lookup_user_ids` mapping, with zero provider calls and no downstream wiring.

#### Verify

Run `uv run pytest tests/test_slack_users.py -q -k 'query or order or deny or lookup'`; run `uv run ruff check <changed Slack Python files>`. Assert field/rank/token behavior, provider-order independence, scope omission, deny secrecy, exact bounds, and zero provider calls through the identity seam.

### M2.4

#### Approach

Exercise the public users boundary, M1-owned credential/scope and policy snapshots, partitioned cache storage, lock behavior, sanitized errors, and result serialization together. Keep all tests offline and limit changes to M2 and genuinely shared M1 seams.

#### Wiring

The agent runner and M1 foundation supply session policy, typed credentials, registration, and shared operation seams. M2 invokes only the defined provider/cache/policy seams and exposes the users result and `lookup_user_ids`; no downstream formatter or other Slack feature is wired here.

#### Edge Cases

- missing credential, missing required scope, configured-but-unconsented email scope, and reauthorization;
- provider authorization, rate limit, transport, malformed JSON, deadline, pagination, cache, lock, and bounded-result errors;
- concurrent agents, changed configuration after session start, partition collisions, and corrupt cache;
- no raw payload, token, hidden identity, or stack trace appears in cache, logs, documentation, or model-visible errors.

#### Tasks

1. Add boundary contract tests for the exact users signature and M1-owned registration seam; do not modify central registration.
2. Add storage permission, atomicity, partition isolation, timestamp/TTL, and cross-process lock tests.
3. Add sanitized assertions for every provider/error category and `Retry-After` parsing.
4. Complete focused Slack user documentation and verify it does not teach raw calls.
5. Run focused and full repository checks without modifying unrelated files.

#### Deliverable

The offline acceptance suite passes for the complete, bounded, scope-aware `slack.users` boundary and its single context-bound identity seam.

#### Verify

Run `uv run pytest tests/test_slack_users.py`; run the relevant shared provider/credential/policy tests; run `uv run ruff check <changed Slack Python files>`; then run `uv run pytest` and `uv run ruff check .`. The acceptance run MUST demonstrate complete pagination, exact 24-hour timestamp semantics, cache partitioning, email reauthorization, bots/deleted/app truth-table behavior, all matching classes, deterministic ordering, exact denylist interaction, no raw persistence, cross-process locking, immediate rate-limit failure, result serialization bounds, and offline seams.

## Acceptance checklist

- Exact async signature and exactly seven safe result fields are implemented.
- `users.list` uses `limit=200`, consumes at most 100 pages and 10,000 users within 60 seconds, and never caches incomplete data.
- OAuth requires `users:read`; `request_email_scope` and `users:read.email` state are explicit, reauthorization is required when configured but ungranted, and cache partitions include email scope state.
- Cache is `slack.users/{team_id}/{user_id}/{email_scope_state}`, versioned, complete-only, atomically written, cross-process locked, and expires exactly 24 hours after successful completion.
- Bots, app users, deleted users, and duplicate conflicts follow the exact truth table and deterministic merge policy.
- Matching uses the exact NFKC/casefold/whitespace/token algorithm and score/order; it never remotely resolves users.
- Public output is capped at 500 records and 262,144 compact JSON bytes without silent truncation.
- Flat denylist resolution is exact, owned at the user resolver seam, fail-closed, and never discloses hidden identities.
- `lookup_user_ids` is the only downstream-consumable seam; no downstream wiring is part of this spec.
- No `users.info`, retry, stale fallback, cursor exposure, token exposure, or raw provider payload is present.
- Focused and full test commands and all provider/cache/policy/clock/lock/serializer seams are specified without network access.