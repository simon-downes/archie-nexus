# 047 — Spec: `slack.thread`

## Objective

Implement the replacement `slack.thread` read function for retrieving a known Slack conversation thread as bounded, safe context. A successful result MUST contain the thread parent first and then replies in chronological order, with normalized authors and message fields, no provider-shaped data, and no message-content cache. Authorization MUST be resolved before the first Slack provider call.

## Context

This is work item M5 of `042-project-slack-personal-assistant.md`. The project is a fresh replacement Slack surface: old provider-shaped functions and aliases are removed, Slack acts through the authenticated user token, and only the seven redesigned functions are public. `slack.thread` is M5 and depends on the completed M1–M4 contracts: M1’s shared foundation and canonical `Message` model, M2’s normalized user cache, M3’s conversation resolution, and M4’s shared message normalization/projection rules. It also uses the shared read policy and sanitized Slack errors.

Slack's `conversations.replies` response commonly includes the parent in the `messages` array and may return replies over several cursor pages. Provider order and duplicate parent records are not a public contract. The function is a bounded context reader, not an export API: it MUST never retrieve or return more than the requested result bound and MUST never persist message content. The project-wide policy also requires immediate rate-limit failure, no automatic retry, and direct deny-list enforcement before provider access. Correct bounded selection is achieved by this validated-provider guarantee: the adapter returns pages in Slack cursor order, each page has at most 100 records, and a nonblank cursor is a complete continuation of the prior page. The service does not assume that order is chronological; it retains the first valid occurrence of each timestamp, identifies the exact parent, and numerically sorts the retained parent/replies. It follows pages until the parent is known and `limit-1` unique replies are retained, or until the validated cursor stream ends; therefore it never claims a bound based on an unvalidated provider ordering.

## RFC2119 Requirements and acceptance criteria

### 1. Public signature and input validation

- `slack.thread` MUST expose exactly this public async signature; it MUST NOT expose a Slack cursor, token, raw request options, or provider response:

  ```python
  async def thread(
      conversation: str,
      thread_ts: str,
      *,
      limit: int = 100,
  ) -> list[dict]:
  ```

  - AC: Introspection or the registered tool schema shows `conversation`, `thread_ts`, and keyword-only `limit`, with the stated defaults and return shape.
  - AC: No valid or invalid invocation can supply a cursor or provider payload through the public API.

- `conversation` MUST be a non-empty string after trimming, and MUST be resolved through the shared conversation resolver to exactly one accessible conversation before Slack access. A conversation ID (`C...`, `G...`, or `D...`) MUST be accepted; a permitted normalized channel/name or DM identity supported by the shared resolver MAY also be accepted. Ambiguous, unknown, malformed, or denied values MUST raise the shared sanitized validation/policy error.
  - AC: Invalid, ambiguous, unknown, and denied conversation values make zero `conversations.replies` calls.
  - AC: The provider receives the resolved conversation ID, never an unresolved display name.

- `thread_ts` MUST be a string matching `^[0-9]{1,12}\.[0-9]{6}$`, MUST represent a finite numeric Slack timestamp greater than zero, and MUST be no longer than 19 characters. Leading/trailing whitespace, signs, exponent notation, timestamps without exactly six fractional digits, non-string values, zero, negative values, and overlong values MUST be rejected before provider access. The value MUST be passed to Slack unchanged after validation.
  - AC: Valid examples include `"1712345678.000001"` and `"1.000000"`; invalid examples include `"1712345678"`, `" 1712345678.000001"`, `"-1.000000"`, `"1.1"`, and `"1e3.000000"`.
  - AC: Timestamp validation does not reject an otherwise well-formed historical or future timestamp based on local wall-clock time; Slack determines whether it exists.

- `limit` MUST be a real `int` (a boolean is invalid) in the inclusive range 1–100. Decimal, string, float, boolean, null, and out-of-range values MUST fail before provider access.
  - AC: `limit=1` and `limit=100` are accepted; `limit=0`, `limit=101`, `limit=True`, and `limit="10"` are rejected without a provider call.

### 2. Authorization and provider operation

- The function MUST obtain the immutable Slack read-policy snapshot and credentials only through existing shared seams. Exact ordering MUST be: (1) local argument validation; (2) load and validate one immutable read-policy snapshot and apply the disabled gate; (3) resolve the conversation through the shared resolver; (4) reject unresolved, ambiguous, unknown, archived, inaccessible, or non-participating targets; (5) evaluate the exact direct deny rule against the canonical conversation ID/type; (6) validate the conversation-type scope from that same snapshot; (7) acquire credentials/client; (8) call `conversations.replies`. No credential acquisition, provider-client construction that can authenticate, conversation/user cache refresh, or Slack API call may occur before steps 2–6. User-cache loading is permitted only after the thread provider succeeds and the complete bounded set is staged. This ordering makes policy and target authorization precede all provider access while allowing the one documented local user-cache refresh only for final enrichment.
  - AC: A denied target produces the policy error and the fake provider records no call.
  - AC: The implementation does not use a broad provider request to discover whether a denied target exists.

- After canonical conversation resolution, the operation MUST validate the conversation-type capability before obtaining/using the provider client: `channels:history` for public channels, `groups:history` for private channels, `im:history` for one-to-one DMs, and `mpim:history` for group DMs. Missing capability MUST raise the shared sanitized reauthorization/scope error naming only missing scope names and MUST make zero `conversations.replies` calls.

- The only message retrieval operation for this function MUST be Slack `conversations.replies`, called with the resolved `channel`, `ts=thread_ts`, and a provider page size of `min(100, max(1, limit))`. The request MUST use cursor pagination only: it MUST NOT send `oldest`, `latest`, or any timestamp-based pagination parameter. The function MUST NOT call `conversations.history`, `users.info` per message, or a legacy `get_thread` function. The shared user-cache loader MAY make its documented complete `users.list` refresh only after retrieval succeeds and only for final enrichment; that is enrichment, not thread retrieval. Provider/transport errors other than rate limits MUST map as follows: authentication failures to the shared authentication error; missing/invalid scope to the shared reauthorization/scope error; explicit Slack permission/not-found/archived/access failures to the shared sanitized resolution/policy error without revealing hidden existence; malformed `ok`/envelope/message data to `SlackProviderResponseError`; invalid cursor/page overflow/repeated cursor/cap/deadline exhaustion to `SlackPaginationError`; pre-response connectivity/HTTP failures to the shared sanitized transport error. No such error is retried or converted to partial success.
  - AC: Recorded requests contain exactly the resolved channel, exact validated `thread_ts`, and an integer page limit no greater than 100; no timestamp pagination parameter is present.

- Parent discovery MUST reserve one result slot. The service collects at most `limit - 1` unique replies plus the exact parent, but MUST continue through cursor pages when the parent has not yet been seen, including for `limit=1`. It MAY inspect a bounded extra page/record set for parent discovery, but MUST never scan beyond `MAX_PAGES`, `MAX_CANDIDATE_RECORDS`, or the 60-second deadline. Once the parent is known and the reserved result capacity is full, it MUST stop before requesting another page. It MUST not stop merely because a page contains `limit` replies while the parent is still absent.
  - AC: `limit=1` finds a parent on a later cursor page without returning a reply; a parent-first page makes one request; and a full reply page without its parent causes only bounded parent discovery, not an unbounded scan.

- The adapter MUST follow `response_metadata.next_cursor` only while the reserved parent/reply bound may still be incomplete. It MUST stop when the parent is known and `limit` unique records are available, or when the cursor is absent/blank. It MUST never expose cursor values or pagination metadata in the result.
  - AC: A multi-page fixture with `limit=3` collects the parent plus only two replies and makes no unnecessary next-page call.
  - AC: A repeated cursor, malformed cursor metadata, 100-page cap hit, candidate-cap hit, or 60-second monotonic operation deadline expiry raises sanitized `SlackPaginationError`; it is never returned as a partial success.

- Rate limits MUST fail immediately. HTTP 429 or a Slack rate-limit envelope MUST raise the shared sanitized `SlackRateLimitError` containing method `conversations.replies` and `retry_after_seconds: int | None`. The shared Retry-After resolver MUST parse only a non-negative integer number of seconds (HTTP header first, then Slack envelope); missing or invalid values MUST become `None`. The function MUST NOT sleep, retry, continue pagination, or use stale/cache data.
  - AC: A rate-limit fixture stops after the rate-limited call, includes only the permitted method/retry fields, and makes no subsequent call.

### 3. Parent, ordering, bound, and edge cases

- The function MUST identify the parent only as the message whose Slack `ts` exactly equals the requested `thread_ts`; it MUST NOT assume the first provider message is the parent. The successful result MUST contain the parent at index 0, followed by replies sorted by ascending numeric timestamp (timestamp string comparison is insufficient). Duplicate messages MUST be removed by exact timestamp, with the first valid normalized occurrence retained. A message whose `ts` equals the requested parent timestamp MUST be treated as the parent even if its subtype is non-`message`; subtype normalization is independent of parent selection.
  - AC: Provider pages in reverse/mixed order and with a duplicated parent produce parent-first, oldest-to-newest replies with one record per timestamp.
  - AC: A page containing replies before the parent does not change output ordering.

- `limit` MUST apply to the complete returned list, including the parent. Therefore `limit=1` returns only the parent; `limit=n` returns at most `n` records. The implementation MUST retain enough provider data to establish the parent and ordering only within the bounded retrieval, and MUST not fetch an unbounded thread to sort it.
  - AC: `limit=1` returns exactly the normalized parent; it makes one request when the parent is on the first page and only bounded additional cursor requests when parent discovery requires them.
  - AC: No successful result has more than `limit` entries, even when Slack reports more replies.

- A valid unthreaded message (`reply_count` absent/zero or no replies returned) MUST return a one-element list containing the parent. An empty provider message list, a missing parent, a parent with a missing/invalid `ts`, or a malformed message/envelope MUST raise a sanitized `SlackProviderResponseError`; the implementation MUST NOT invent a parent or ordering. A valid envelope MUST have `ok: true`, `messages` as a list, and `response_metadata` absent or an object. `next_cursor` MUST be absent, blank, or a non-empty string of at most 512 Unicode code points; non-string, oversized, or malformed cursor metadata MUST raise `SlackPaginationError`. A page MUST contain no more than 100 records and the invocation MUST inspect no more than 10,000 candidate records; exceeding either bound is an error, not truncation.
  - AC: An unthreaded fixture returns `[parent]` without requiring a second call.
  - AC: Missing or malformed parent fixtures fail rather than returning the first reply or `[]`.

- The adapter MUST validate success markers, `messages` list shape, message timestamp shape, cursor metadata, and response bounds. It MUST reject an individual record whose timestamp is absent, malformed, non-string, or outside the bounded Slack timestamp format rather than exposing it.
  - AC: Malformed pages and malformed timestamps produce a sanitized response/pagination error without raw provider data.

### 4. Exact normalized public result

- Every returned dictionary MUST be a public projection of the canonical shared `Message` model owned by M1, not a second internal message model. The canonical model has exactly these fields (nullable values are present as `None`): `id`, `conversation_id`, `ts`, `author`, `text`, `subtype`, `thread_ts`, `reply_count`, and `permalink`. The thread projection MUST map those fields explicitly: `id` → `id`, `conversation_id` → omitted because the requested conversation is implicit, `ts` → `timestamp`, `author` → `author`, `text` → `text`, `subtype` → `type` (using `message` when `subtype` is `None`), `thread_ts` → `thread_timestamp`, `reply_count` → `reply_count`, and `permalink` → `permalink`. The parent-first/reply ordering requirement below is a projection of this canonical model; it MUST NOT redefine the model or add thread-specific message fields.

  ```python
  {
      "id": str | None,
      "type": str,
      "timestamp": str,
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

  The nested `author` dictionary MUST also have exactly the listed keys. The canonical model and this projection MUST use the shared safe-text, subtype, timestamp, author, reply-count, and permalink rules from `slack.messages`; unavailable nullable values remain `None`, and raw provider fields MUST NOT appear. The projection MUST be JSON serializable.
  - AC: Snapshot tests assert exact top-level and author key sets and JSON-serializability.
  - AC: Blocks, attachments, files, reactions, profile objects, emails, raw provider fields, and arbitrary future fields never appear in output.

- Authors MUST be enriched by joining IDs against the normalized user cache from `slack.users` in one local/bulk operation. Author precedence is exact and shared with `slack.messages`: use a valid non-empty `user` ID first; otherwise use a valid `bot_id`; otherwise use a valid `app_id`; otherwise, only when the message has no stronger author field, use the first valid string in `reply_users` in provider order; otherwise author ID is `None`. `reply_users` never overrides `user`/`bot_id`/`app_id` and is not emitted as a list. The function MUST NOT issue one `users.info` call per author. The shared lookup seam is `lookup_user_ids(ids)` (or its exact established equivalent): it returns normalized records for hits and `None` for misses, and a miss is non-fatal and MUST NOT trigger a provider call. An unknown, deleted, or bot author MUST retain its stable Slack ID and nullable identity fields/status where known, using canonical `is_deleted`; it MUST not cause the message to disappear. No `users.info` fallback is permitted.
  - AC: A fixture with known, unknown, deleted, and bot authors returns IDs for all and correct nullable/status fields, with zero `users.info` calls.
  - AC: User-cache misses do not become provider calls or errors solely because the display join is incomplete.

- The parent and replies MUST use the same normalized schema. A parent MUST have `thread_ts` equal to the requested timestamp in the successful output, even if Slack omitted that field; a reply MUST retain its valid provider thread timestamp. The function MUST not synthesize text, author IDs, permalinks, or provider fields that are unavailable. The parent’s `reply_count` is retained only when it is a non-negative real integer; malformed supplied values are normalized to `None` under the optional-field rule, while malformed required envelope/message/timestamp fields fail.
  - AC: Parent normalization is stable when Slack omits the parent `thread_ts`; unavailable optional values remain `None`.

### 5. Privacy, caching, errors, and replacement

- Message and thread content MUST NOT be written to the metadata cache, session metadata cache, or any new persistent store. User metadata may be read from the shared normalized user cache, but raw Slack responses MUST never be persisted.
  - AC: A successful call leaves no message/thread cache entry and a second call performs provider retrieval unless an unrelated existing cache seam explicitly applies only to users/conversations.

- All validation, authorization, authentication, transport, malformed-response, pagination, cache, and rate-limit failures MUST use the existing sanitized Slack error categories. Errors MUST NOT include tokens, raw response bodies, block payloads, message text, URLs containing secrets, deny-list entries, or hidden-target existence/count information.
  - AC: Error assertions verify category and permitted fields, not provider-shaped text.

- Replace old `get_thread` tests/docs only where they are the obsolete thread surface; central registration state remains owned by the shared Slack foundation. Only the redesigned `slack.thread` contract may be publicly registered, and the central seven-function allowlist MUST contain it alongside the other six functions with no duplicate namespace or compatibility alias. Unrelated Slack functions and shared OAuth behavior MUST remain unchanged.
  - AC: Registration tests show `slack.thread` and no public `get_thread`; documentation contains the exact signature, schema, bounds, ordering, denial, no-cache, and rate-limit behavior.

## Technical Design

### Overview

Implement a thin `conversations.replies` adapter plus a service-layer thread assembler. The service validates inputs, evaluates read policy, resolves the conversation, retrieves bounded pages, validates and deduplicates provider messages, joins normalized users, and emits the exact safe projection. The adapter owns request construction, response-envelope validation, cursor/deadline safety, and sanitized provider errors; it never returns raw envelopes to the public formatter.

The design deliberately treats the parent timestamp as the identity key and the complete output bound as including the parent. It does not use a cache for messages and does not make a second provider call merely to enrich an author.

**Done when:** the request path, ownership boundary, parent rule, and bounded stopping rule are unambiguous.

### Technical stack and existing seams

- Current-repo foundation dependencies are explicit: use Python 3.13 and the workspace's existing `agent` package (`archie_agent.exec.tools`), shared package (`archie_shared`), `httpx`/async transport and `pydantic` versions already locked in `pyproject.toml`/`uv.lock`; use the existing async tool registration, Slack OAuth credential path, sanitized error conventions, monotonic clock/deadline utility, and metadata/user loader. Add no dependency, no SDK, no second credential store, and no message-content cache.
- Follow the Slack service-tool patterns in the existing agent tool package and the normalized/cache/error decisions in specs 042, 044, 045, and 046. If those shared modules are introduced by an earlier slice, extend them rather than creating parallel Slack clients or policy evaluators.
- Ownership is explicit: the shared Slack foundation owns the central namespace, exact seven-function allowlist, OAuth/client boundary, immutable policy snapshot, scope validation primitives, and shared error taxonomy; `slack.conversations` owns canonical conversation resolution and the conversation metadata cache; `slack.users` owns the normalized user cache and `lookup_user_ids` seam; this spec owns only the thread service, `conversations.replies` mapping extension, normalized thread projection, focused tests, and focused documentation. It MUST not create a second registration, credential store, policy evaluator, conversation resolver, or user lookup path. The expected implementation seams are a Slack thread service/formatter, shared Slack client method for `conversations.replies`, shared conversation/policy resolver, shared user-cache loader, and the existing registration/skill documentation seam. Exact module names MUST follow the actual package layout discovered during implementation and MUST be recorded in the change.

**Done when:** every technology choice is an existing dependency or explicitly states that no new dependency is required, and each shared seam has a named owner.

### Architecture and data flow

1. Validate argument types and the exact timestamp/limit grammar; fail before policy, credentials, or provider access.
2. Load the immutable read-policy snapshot, resolve/authorize the conversation, and validate its required history scope; deny, disabled policy, resolution failure, or missing scope ends the flow before any provider API call.
3. Acquire credentials through the shared seam, then call `conversations.replies` with only `channel`, `ts`, optional cursor, and page limit `min(100, limit)`. Track seen cursors, page count, candidate count, deadline, and timestamp keys internally.
4. Validate each envelope/page and normalize only safe message fields. Select the exact parent by `ts == thread_ts`; reserve one output slot for it and retain at most `limit - 1` replies until the parent is known. Continue bounded cursor discovery while the parent is absent or the reserved result is incomplete.
5. Bulk-join authors from the normalized users cache, sort the parent separately then replies by numeric timestamp, truncate to `limit`, and emit the exact projection.
6. On any collection/provider error, discard staged records and return the typed sanitized error; do not cache or return partial data.

**Done when:** happy path, deny path, pagination stop, malformed response, and rate-limit path each have an explicit terminal behavior.

### Data model and normalization

Consume the shared canonical `Message` model owned by M1 with exactly `id`, `conversation_id`, `ts`, `author`, `text`, `subtype`, `thread_ts`, `reply_count`, and `permalink`; M5 MUST NOT define a parallel `NormalizedMessage` model or retain a raw provider object. Parent-first/reply ordering and the public field renaming are projections of that canonical model. Use the shared timestamp parser and normalization rules, comparing `(seconds, micros)` numerically. Deduplication is keyed by the canonical timestamp identity used for this thread; conflicting duplicates retain the first valid canonical record.

The author join consumes only the normalized user projection/status-bearing cache (`id`, names, `is_bot`, `deleted`). Missing joins produce a stable ID plus null fields. No message model or serialized message content has a persistence lifecycle.

**Done when:** all public fields have explicit types/nullability, the parent identity and dedup key are defined, and no raw-provider reference can cross the formatter boundary.

### Pagination, bounds, and errors

Define named constants for `MAX_LIMIT=100`, `MAX_PAGE_SIZE=100`, `MAX_PAGES=100`, `MAX_PAGE_RECORDS=100`, `MAX_CANDIDATE_RECORDS=10_000`, `MAX_CURSOR_LENGTH=512`, and `OPERATION_DEADLINE_SECONDS=60`. The adapter must detect blank/absent completion, repeated cursors, malformed cursor metadata, page overflow, candidate overflow, and deadline expiry. It must not fetch a page when the result capacity is full. A response that cannot establish the requested parent is a provider-response error, not an empty result.

For `limit=1`, the first valid page may establish and return the parent; if the parent is absent from that page, continue only when a valid cursor exists and the operation remains bounded, then fail if the complete bounded retrieval cannot establish it. This preserves the parent contract without an unbounded scan. Provider rate limits are mapped immediately and never retried.

**Done when:** every loop has a numeric page/result/deadline bound and every named failure has a typed sanitized outcome.

### Code structure, registration, tests, and docs

- Extend the shared Slack client with `conversations.replies` request/response mapping and error handling; do not place denylist logic in the provider client.
- Add the public thread service and normalized projection in the Slack tooling package, reusing shared conversation resolution, policy, user cache, and error types.
- Replace old registration and skill guidance with the `slack.thread` function. Do not modify unrelated providers or add compatibility aliases.
- Add `tests/test_slack_thread.py` with fake policy/resolver/cache/provider seams and no network dependency. Include exact schema snapshots and call assertions.
- Update the Slack skill/reference documentation and any registration documentation required by the existing architecture. Documentation MUST not contain credentials or raw Slack-call instructions.

**Done when:** each implementation, test, and documentation touch point is named and the test boundary is deterministic and offline.

### Key decisions

- **`conversations.replies` only, not history:** the function is given a known thread timestamp, so broad history would violate the thread boundary.
- **Parent included in `limit`:** this makes the result bound a hard public maximum and makes `limit=1` useful for parent-only context; treating limit as replies-only was rejected because it can return 101 records.
- **Exact timestamp grammar:** six fractional digits and bounded integer seconds match Slack timestamps while rejecting coercion and oversized input; arbitrary decimal parsing was rejected because it permits ambiguous provider requests.
- **Parent by exact timestamp, not first item:** Slack page order is not a contract; first-item selection was rejected because it can mislabel a reply.
- **No stale/partial fallback:** thread content is not cached and incomplete retrieval cannot safely claim ordering; fallback was rejected to avoid hidden or misleading context.
- **Bulk local author join:** user-cache enrichment avoids N+1 calls and keeps the provider operation limited to thread retrieval; per-author `users.info` was rejected.

**Done when:** each decision names the rejected alternative and ties the choice to a concrete thread, privacy, or bound constraint.

## Milestones

### M5.1 — Contract, shared seams, and validation

**Approach**
- Confirm and reuse the current-repo foundation seams before implementing the public function; keep validation and authorization orchestration in the thread service.

**Wiring**
- The central Slack foundation supplies registration, credentials, policy snapshot, scope/error types; conversations supplies canonical resolution; users supplies the normalized lookup seam. Tests inject fakes at each seam.

**Edge Cases**
- Cover disabled/malformed policy, unresolved/ambiguous/archived/denied targets, non-string or whitespace inputs, timestamp grammar boundaries, boolean limits, and missing scopes; all terminate before provider access.

**Tasks**
1. Confirm the existing Slack registration, OAuth/client, policy, conversation resolver, user-cache, and sanitized-error seams against specs 042/044/045/046.
2. Define the exact public signature, result/author schema, timestamp parser, `limit` validator, named pagination constants, and typed failure mapping.
3. Add pre-call tests for malformed conversation, denied/read-disabled access, malformed timestamps, and all limit boundary/type cases.

- No invalid input or denied target reaches a provider fake.
- Contract tests enforce exact signature, timestamp grammar, and limit 1–100 behavior.

**Deliverable**
- One passing contract-test report proving the public signature and zero-provider-call authorization/validation boundary.

**Verify**
- `uv run pytest tests/test_slack_thread.py -q -k 'validation or deny or contract'`

### M5.2 — Provider adapter and bounded retrieval

**Approach**
- Implement the validated cursor-stream adapter and service selection algorithm: retain first valid unique timestamps, reserve the parent slot, continue until the exact parent plus `limit-1` replies are known, then numerically sort and truncate; never rely on provider order.

**Wiring**
- The shared client maps canonical channel/ts/limit/cursor requests and returns validated private records; the service owns bounds, cursor state, deadline, and typed error mapping.

**Edge Cases**
- Exercise parent on later pages, limit 1, duplicate/conflicting timestamps, repeated/malformed cursors, page/candidate/deadline caps, 429, authentication, scope, permission, transport, and malformed envelopes.

**Tasks**
1. Implement cursor-only `conversations.replies` request mapping with resolved channel, exact thread timestamp, no `oldest`/`latest`, and `min(100, limit)` page limits.
2. Implement envelope/message/subtype validation, timestamp-key deduplication, parent reservation and later-page discovery (including `limit=1`), cursor tracking, 100-page/10,000-candidate caps, 60-second deadline, and stop-at-bound behavior.
3. Map scope, rate limits, and provider/transport/pagination failures to sanitized errors with no retries or stale fallback.

- Multi-page fixtures stop at the requested bound while reserving parent discovery, never expose cursors, and never issue an unnecessary page; request fixtures prove cursor-only pagination.
- Repeated/malformed cursors, malformed pages/subtypes, missing parent, missing scope, deadline/cap exhaustion, and rate limits fail with the required category and no raw data.

**Deliverable**
- One deterministic provider-adapter test report showing exact request ordering, bounded parent-first selection, and typed failures for every non-rate-limit mapping.

**Verify**
- `uv run pytest tests/test_slack_thread.py -q -k 'pagination or provider or rate'`

### M5.3 — Normalization, ordering, and identity joins

**Approach**
- Apply the shared messages projection rules to the private normalized records, then perform one bulk author join and emit only the exact public schema.

**Wiring**
- The formatter consumes staged normalized records and the users lookup result; it has no provider or persistence dependency. Parent selection precedes reply sort, and the inclusive limit is applied after deduplication.

**Edge Cases**
- Cover every safe subtype, unsafe text/permalink, missing optional fields, numeric timestamp ordering, duplicate timestamps, unthreaded parents, and `user`/`bot_id`/`app_id`/`reply_users` precedence with unknown, deleted, and bot users.

**Tasks**
1. Implement normalized message and author projections with nullable optional fields and raw-field exclusion.
2. Perform one bulk normalized-user-cache join, retaining unknown/bot/deleted IDs and status fields without `users.info` calls.
3. Implement parent-first plus numeric chronological reply ordering, duplicate removal, unthreaded-parent behavior, and the inclusive result bound.

- Fixtures verify exact schema, safe subtype/message normalization, parent-first order, chronological replies, numeric timestamp sorting, deduplication, `limit=1` later-page parent discovery, unknown/deleted/bot authors including `reply_users`, and non-fatal user-cache misses with no `users.info` calls.
- No message content or raw payload is written to any cache.

**Deliverable**
- One exact-schema normalization fixture report proving parent-first chronological output, shared safe-field rules, author precedence, and no message-cache writes.

**Verify**
- `uv run pytest tests/test_slack_thread.py -q -k 'ordering or normalize or author or cache or unthreaded'`

### M5.4 — Replacement surface, documentation, and full verification

**Approach**
- Wire the completed service through the existing central registry and document the final contract without changing unrelated Slack surfaces.

**Wiring**
- The foundation remains the sole namespace/allowlist owner; this slice contributes only the thread callable, focused tests, and focused documentation.

**Edge Cases**
- Verify no `get_thread` alias, duplicate registration, credential/documentation leak, live network dependency, or mismatch between code, docs, and shared messages rules.

**Tasks**
1. Replace `get_thread` registration/tests/docs with `slack.thread`; remove compatibility aliases without changing unrelated Slack functions.
2. Document the exact signature, validation, result schema, ordering, pagination bound, deny pre-call, no-cache rule, and rate-limit behavior.
3. Run the complete focused test file and relevant Slack registration/shared-seam tests; inspect the diff to ensure only this plan's requested code/tests/docs are changed during implementation.

- Public registration exposes `slack.thread` only, and documentation matches this plan exactly.
- All requirements above have passing automated coverage and no live network dependency.

**Deliverable**
- One final focused verification report containing the registered seven-function surface, documentation contract check, full thread test result, and lint result.

**Verify**
- `uv run pytest tests/test_slack_thread.py -q`
- `uv run pytest -q` (or the repository's documented focused Slack/registration test command if the full suite is unavailable)
- `uv run ruff check .` (or the repository's documented lint command)
