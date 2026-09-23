# 043 — Spec: `slack.search`

## Objective

Implement `slack.search` as Archie’s bounded Slack context-retrieval function. It MUST use Slack Real-time Search (`assistant.search.context`) for one request per invocation, return safe normalized results, and enforce the authenticated user’s local read policy before data becomes model-visible. Messages are the default search surface; files, channels, and users are explicit opt-ins.

## Context

Project `042-project-slack-personal-assistant.md` replaces the legacy Slack wrapper with a seven-function personal-assistant surface. This slice is consumed by model-authored exec code as `await slack.search(...)`; it is an internal user-token integration, not an administration or export interface. M1 owns the complete shared Slack foundation and the search vertical slice: the immutable operation context, authoritative policy shape and enforcement, resolver, normalized user/conversation identity, canonical `Message` model, metadata caches, credentials and scope validation, async client, sanitized errors, and registration. Search consumes those seams; it does not depend on any later Slack slice or establish parallel seams. Search results and message content MUST never be cached.

The repository currently has no Slack tool package or search tests to preserve. The implementation MUST follow the existing async `@tool` registration in `agent/src/archie_agent/exec/tools/__init__.py`, the runner’s typed-error boundary, and the adjacent Slack plans. M1 is the prefactor and first vertical slice, not a foundation-only phase: it owns the exact resolver, metadata-cache, client, policy, error, credential/scope, canonical-model, operation-context, and registration seams required by search. Search consumes those seams and has no dependency on a later foundation or later Slack slice. The public registration owner is `agent/src/archie_agent/exec/tools/slack/__init__.py`; importing that package from `get_all_tools()` is the only registration path. No legacy search alias may remain.

Slack’s sole search endpoint is `POST https://slack.com/api/assistant.search.context`. Its logical request fields are `query`, `channel_types`, `content_types`, `context_channel_id`, `after`, `before`, `limit`, `sort`, `sort_dir`, and `include_context_messages`. The transport is owned by `slack/client.py`: an HTTPS POST with JSON body, the existing authenticated user token in the standard `Authorization: Bearer ...` header, and `Content-Type: application/json`; the token MUST never be present in the logical payload, logs, exception text, or result. Optional fields are omitted rather than sent as null. This spec deliberately uses no cursor, legacy `search.messages`, semantic continuation, raw blocks, highlights, or raw provider envelopes.

## RFC2119 Requirements

### Public contract and validation

- `slack.search` MUST expose exactly this async signature, apart from the decorator/registration mechanism:
  ```python
  async def search(
      query: str,
      *,
      conversation: str | None = None,
      content_types: list[str] | None = None,
      channel_types: list[str] | None = None,
      after: str | None = None,
      before: str | None = None,
      sort: Literal["relevance", "timestamp"] = "relevance",
      include_context: bool = True,
      limit: int = 20,
  ) -> list[dict]
  ```
  **AC:** The registry contains `slack.search`, not `slack.search_messages` or another compatibility alias; it is awaitable and returns a JSON-serializable list, including `[]`.
- `query` MUST be a non-empty, trimmed string of at most 1,000 Unicode code points. Boolean and non-string values MUST be rejected locally. The trimmed value MUST be sent, not the untrimmed spelling.
  **AC:** Empty, whitespace-only, wrong-type, and 1,001-code-point queries make zero provider calls; a valid surrounding-whitespace query sends its trimmed value.
- `limit` MUST be a real integer (not `bool`) from 1 through 20 inclusive. `sort` MUST be `relevance` or `timestamp`. `include_context` MUST be a real boolean.
  **AC:** Invalid values make zero RTS calls; the exact limit is sent and the final list never exceeds it.
- `relevance` MUST map to RTS `sort="score", sort_dir="desc"`; `timestamp` MUST map to `sort="timestamp", sort_dir="desc"`.
  **AC:** No other sort value reaches Slack, and timestamp mode is newest-first after local normalization.

### Scope, capabilities, and exact RTS call

- Omitted `content_types` MUST mean exactly `['messages']`. A supplied list MUST be a non-empty duplicate-free list containing only `messages`, `files`, `channels`, and `users`. Omitted `channel_types` MUST mean exactly `['public_channel', 'private_channel', 'im', 'mpim']`; a supplied list MUST be non-empty, duplicate-free, and limited to those values. Caller order MUST be preserved in the logical payload.
  **AC:** Default requests contain exactly those arrays; files/channels/users are never searched unless explicitly supplied; invalid arrays make no request.
- The service MUST make exactly one RTS request for every successful invocation that reaches the provider. The JSON logical payload MUST contain exactly the validated `query`, `channel_types`, `content_types`, `limit`, mapped `sort`/`sort_dir`, `include_context_messages`, and applicable bounds/direct-target field. It MUST never contain cursor, pagination, retry, token, provider options, or unrelated null fields.
  **AC:** A spy sees one POST to `assistant.search.context`, one JSON body, no second search/enrichment request, no cursor, and no legacy method call.
- `conversation`, when supplied, MUST be a non-empty trimmed string of at most 200 Unicode code points and MUST be resolved by the shared direct-target resolver seam (`resolve_conversation_reference`) to one canonical ID and safe metadata before RTS access. The ID MUST be sent as `context_channel_id`; the default channel types remain unchanged unless explicitly supplied.
  **AC:** Unknown/malformed targets produce sanitized not-found/validation errors; a locally denied target produces a policy error and zero RTS calls. The resolver accepts only its documented ID/name/DM-reference forms and does not invent a search syntax.
- Metadata calls are permitted only through shared cache/resolver seams before or after the single RTS call: a complete conversation-cache refresh and one bulk user-cache refresh/join are allowed as needed. They MUST not call RTS, search per result, call `users.info` per result, or alter the one-RTS invariant. Broad-search filtering MAY use cache refresh after RTS only if the cache is incomplete; search content is never written to cache.
  **AC:** Call-record tests distinguish one RTS call from permitted `users.conversations`/bulk metadata refreshes and prove no per-result provider calls.
- Capability requirements MUST be granular at the service boundary: `search:read.public` for `public_channel`, `search:read.private` for `private_channel`, `search:read.im` for `im`, `search:read.mpim` for `mpim`, `search:read.files` for `files`, and `search:read.users` for `users`; `messages` requires the channel capabilities implied by each requested channel type. These are the canonical capability names even though the current shared Slack OAuth configuration requests the older bundled history/read scopes (`channels:history`, `channels:read`, `groups:history`, `groups:read`, `im:history`, `mpim:history`, `users:read`, `search:read`). The shared credential adapter MUST translate a granted provider scope to the canonical capabilities only when its documented implication is exact; it MUST NOT treat bundled `search:read` as all granular capabilities unless Slack explicitly reports that grant. The shared OAuth configuration/reauthorization path MUST be extended to request the union needed by the canonical matrix, while an already-stored token is never silently broadened. A request MUST fail closed with `SlackReauthorizationRequiredError` listing the sorted missing canonical scope names (and no token or response body).
  **AC:** Each content/channel combination checks only its required scopes; missing one scope prevents RTS access and identifies the required reauthorization scope set without leaking credentials.
- An existing stored user token MUST be reused; this operation MUST NOT mint, silently replace, broaden, or retry with another token. Missing scopes/capability MUST raise `SlackReauthorizationRequiredError` (a sanitized auth/configuration error) and MUST leave the existing token unchanged. Invalid/expired credentials MUST raise the shared authentication error. There is no automatic OAuth flow in a tool call.
  **AC:** Scope and expired-token fakes make zero RTS calls, preserve the token fixture, and expose only method/scope-safe error data.

### Operation context, deadline, and cache partition

- Every invocation MUST receive one immutable shared Slack `OperationContext` containing the authenticated principal/token identity (never the token value), an immutable read-policy snapshot, a monotonic deadline, correlation-free operation metadata, and the credential/scope validator. M1 owns this context seam and passes the same context/deadline through policy, resolver, cache, credential validation, and client boundaries. The public signature MUST NOT expose context, deadline, token, or refresh arguments. The context deadline is 60 seconds from service entry and applies to validation-adjacent cache/resolver work, any permitted metadata refresh, and the single RTS attempt; no wait, retry, or new provider call may start after expiry. Deadline exhaustion before RTS is a sanitized transport/deadline error; expiry during or after request handoff is a transport error with no retry.
- Metadata-cache partition identity MUST be exactly `(provider="slack", authenticated_principal_id, workspace/team_id, cache_kind, schema_version)`, with policy snapshot and query excluded. `cache_kind` is `conversations` or `users`; conversation entries are additionally keyed by canonical conversation ID and user entries by canonical user ID. A token string, search query, result, message text, or policy deny contents MUST never be a partition key or cache value. A changed principal, workspace, or schema version MUST be a cache miss and MUST NOT use another partition.
  **AC:** Fakes record the same operation context/deadline at resolver, cache, and client boundaries; expired deadlines stop work, and partition tests prove cross-user/workspace/schema and query/result isolation.

### Time semantics

- `after` and `before`, when supplied, MUST be strings containing timezone-aware ISO-8601 timestamps with an explicit offset. Naive, invalid, boolean, and non-string values MUST be rejected locally. Values MUST be converted to UTC instants with microsecond precision for local comparison; the outbound logical values MUST be integer Unix seconds (floor of the UTC instant), because RTS requires Unix timestamps.
  **AC:** Offset fixtures assert exact UTC integer seconds in the request and preserve microseconds internally for filtering.
- Bounds MUST be exclusive: `timestamp <= after` and `timestamp >= before` are omitted locally, even if RTS returns them. Both bounds MUST satisfy `after < before` using the precise instants. The query MUST NOT be rewritten with Slack `after:`/`before:` terms.
  **AC:** Offset normalization, equality exclusion, sub-second precision, equal bounds, and reversed bounds are deterministic and tested.

### Exact RTS success envelope and content mappings

- A successful RTS response MUST be a JSON object with `ok: true` and a `results` array (an absent, null, or non-array `results` member is a malformed-success response). Each result MUST be an object with a recognized `type` and the required fields in the table below; unknown top-level keys are ignored. `context_messages`, when present, MUST be an object whose `before` and `after` members are arrays (missing members mean empty arrays). A response with `ok: false`, a non-object envelope, or a non-boolean `ok` is not a success and follows the error table below. Message records MUST normalize into M1’s canonical `Message` model; search MUST NOT define a search-specific message shape.

| RTS `type` | Required safe source fields | Canonical ID | Required identity / output mapping |
|---|---|---|---|
| `message` | non-empty `id` or valid `ts`; valid `ts`; safe `text`; `channel_id` or explicit conversation object | non-empty `id`, else normalized `ts` | `text`, `ts`, `user`/author, conversation, and validated `permalink`; missing/invalid conversation discards the record |
| `file` | non-empty `id`; safe `title` or `name` | `id` | `text` is bounded title, else name; conversation only when the record explicitly says it belongs to the direct target; otherwise discard |
| `channel` | non-empty `id`; safe `name` | `id` | `text` is bounded name; conversation identity is the channel identity, normalized to channel/dm/group_dm; no guessed identity |
| `user` | non-empty `id`; at least one safe display/real/username value | `id` | `text` uses deterministic display-name, real-name, username precedence; `conversation=None`; no message identity is required |

- M1’s canonical internal `Message` model is exactly these fields (nullable values are present as `None`):
  ```python
  {
      "id": str | None,
      "conversation_id": str,
      "ts": str,
      "author": dict,
      "text": str,
      "subtype": str | None,
      "thread_ts": str | None,
      "reply_count": int | None,
      "permalink": str | None,
  }
  ```
  Search MUST first normalize every message/context record into this shared model using the shared normalizers; it MUST NOT define, rename, omit, or add canonical message fields. `author` is built from the allowlisted `user`/author object or normalized user-cache join; an absent author is `None` only if the record otherwise has valid message identity. For file/channel/user rows, message-only canonical fields do not apply. Context records use the same canonical `Message` model and never inherit a type or fields from another content type.
  **AC:** Envelope fixtures distinguish missing `results`, each required field, all four mappings, unknown keys, and context shape; exact output keys and values are asserted.

### Safe result schema, mappings, and limits

- Every returned primary item MUST have exactly these keys, with `None` for unavailable optional values:
  ```python
  {
      "type": "message" | "file" | "channel" | "user",
      "id": str,
      "conversation": {"id": str, "name": str | None,
                       "type": "channel" | "dm" | "group_dm"} | None,
      "timestamp": str | None,
      "author": {"id": str, "username": str | None, "real_name": str | None,
                  "display_name": str | None, "is_bot": bool | None,
                  "is_deleted": bool | None} | None,
      "text": str | None,
      "permalink": str | None,
      "context": {"before": list[dict], "after": list[dict]} | None,
  }
  ```
  **AC:** Key sets are exact, values are JSON-safe, and an empty successful response is `[]`.
- Message mapping MUST first produce M1’s canonical internal `Message`: `id` is the RTS stable result/message ID when it is a non-empty safe string, otherwise the normalized timestamp; `conversation_id` is the validated canonical conversation ID; `ts` is the canonical UTC timestamp; `author` is the shared normalized author projection; `text` is the safe normalized provider text; `subtype` is the validated subtype or `None`; `thread_ts` is the validated provider thread timestamp or `None`; `reply_count` is the validated non-negative integer or `None`; and `permalink` is the validated HTTPS permalink or `None`. Search’s public primary result is a separate projection, not a canonical message shape: it maps `id`, `ts`, `author`, `text`, and `permalink` to public `id`, `timestamp`, `author`, `text`, and `permalink`, and separately adds the authorized `conversation` object and `context`; it may expose `timestamp`/`thread_timestamp` and MUST NOT expose `conversation_id` or `ts` as extra public keys. This projection mapping is required and is not prohibited by the shared canonical-model boundary. File mapping MUST use file ID and only safe file title/name in `text`; channel mapping MUST use channel ID and safe channel name in `text`; user mapping MUST use user ID and safe display/real/username text. File/channel/user results have `context=None`; their timestamp/author/permalink are `None` unless the allowlisted provider record supplies a safe value. A user result has `conversation=None` because it is not conversation content; this is the sole result-type exception to the conversation-identity rule.
  **AC:** Fixtures for all four types assert exact field mappings and prove raw file/profile/channel metadata is absent.
- A message/context entry MUST contain only `id`, `timestamp`, `author`, `text`, and `permalink`; it MUST never contain nested context. Context entries MUST use the same safe author/permalink projection as messages.
  **AC:** Context output contains no blocks, attachments, file/profile fields, or recursive context.
- IDs MUST be non-empty strings of at most 256 code points; conversation names, usernames, real names, display names, titles, and safe text MUST be bounded to 256, 256, 256, 256, 512, and 4,000 code points respectively. Text truncation MUST be deterministic at a Unicode code-point boundary and MUST not add raw provider markup. At most 10 `before` and 10 `after` context entries may be returned per primary result. The normalized serialized response MUST be at most 12,000 UTF-8 bytes; if necessary, context entries are removed from the end (`after` then `before`) before primary results, and primary results are then removed from the end, never partially serialized or emitted over the bound.
  **AC:** Oversized fields are bounded, collections are capped, and a byte-count test proves the returned JSON is at most 12,000 bytes.
- Slack timestamps MUST normalize to canonical UTC ISO-8601 with `Z`, retaining provider precision through microseconds (or the available precise fractional digits, capped at six). Invalid timestamps omit the malformed entry. Permalinks MUST be retained only when absolute HTTPS URLs with no credentials, fragments, or unsafe control characters; otherwise `None`.
  **AC:** UTC conversion/precision and unsafe permalink fixtures produce exact expected values.
- A primary result without a safe conversation ID MUST be discarded, except a valid `user` result as defined above. A file/channel result MAY inherit the resolved direct target conversation identity only when RTS explicitly marks it as content belonging to that target; it MUST NOT inherit merely because the call was broad. A message’s conversation identity MUST come from its own safe `channel_id`/conversation object, or from the explicit direct target when RTS marks the message as target-context. Context entries MUST inherit the primary message conversation only when RTS supplies no identity and the context record is explicitly attached to that primary; an independently identified context entry MUST match the primary ID or be discarded. Identity MUST never be guessed from names or neighboring results.
  **AC:** Missing, conflicting, cross-conversation, and direct-target inheritance fixtures prove fail-closed behavior.
- Results MUST preserve RTS order in relevance mode. Timestamp mode MUST sort descending by precise normalized timestamp, then stable ID ascending. Duplicate `(type, id)` primaries and duplicate context entries MUST appear once. Local policy, identity validation, normalization, deduplication, and time filtering happen before the final limit is applied.
  **AC:** Provider-order, ties, duplicates, malformed entries, and post-filter limit tests are deterministic.

### Context, policy, cache, and errors

- The authoritative Slack policy snapshot MUST have exactly `{ "enabled": bool, "read": { "enabled": bool, "deny": list[str] }, "write": { "enabled": bool, "deny": list[str] } }`. The top-level `enabled` and both nested `enabled` values are real booleans; both deny lists are flat lists of strings. Defaults are top-level/read enabled and write disabled, with omitted deny lists defaulting to empty. Extra keys, missing required keys after defaulting, aliases, nested rules, wildcards, and non-string entries are invalid and fail closed. Search uses only `read.enabled` and `read.deny`; it MUST still validate the complete shared snapshot, including the write block, through M1’s policy seam before any provider access.
  **AC:** The exact policy shape, defaults, independent read/write gates, malformed values, extra keys, and fail-closed behavior are tested without disclosing deny entries.
- `include_context=True` MUST map to `include_context_messages=True` and normalize `context_messages` into `context.before`/`context.after`. Context records MUST first use M1’s canonical `Message` model, with canonical `conversation_id` and `ts` used for authorization, comparison, and deduplication; the public nested result is a separate projection containing exactly `id`, `timestamp`, `author`, `text`, and `permalink`. It never contains nested context, canonical `conversation_id`/`ts`, or unrelated message fields. Context receives the same time bounds, identity checks, broad deny filtering, safe-field limits, and deduplication as primaries. Context identity is inherited from the already-validated primary result only when the provider marks the record as attached to that primary and supplies no identity of its own: internally it inherits the primary canonical `conversation_id` exactly, while the context message retains its own valid canonical `ts`. The inherited `conversation_id` and `ts` MUST NOT be guessed, rewritten, or exposed as extra public context keys. If context supplies an identity or `ts`, it MUST agree with the primary conversation attachment rules; a conflicting identity is discarded. A context message’s own valid timestamp remains required; the primary `ts` is never copied as the context message’s `ts`. If the primary is discarded for malformed data, policy, time, or identity, its entire context is discarded. A denied or identity-less context entry is silently omitted, without disclosing its ID, name, reason, or count.
  **AC:** Context is absent when false, safely projected when true, and denied/cross-conversation entries never appear.
- Read policy MUST use the shared flat `read.deny` snapshot: exact case-insensitive `C...`, `G...`, or `D...` IDs; exact `#name`; and exact one-to-one DM participant username/real-name/display-name/email matches. Group DMs are denied only by conversation ID. Broad results MAY be requested from RTS and MUST then be deny-filtered locally, including context, before normalization becomes model-visible. For every affected record, missing or incomplete conversation metadata is a deny match for broad-search purposes (fail closed): drop that primary and its affected context subtree, do not attempt name/participant inference, and do not refresh-and-retry RTS. A complete metadata refresh may be performed once before filtering; if it fails or remains incomplete, all records whose policy identity cannot be proven are dropped. Direct-target requests use the already resolved complete identity and fail with the direct policy error before RTS when it cannot be proven. Hidden identities and counts MUST not be disclosed.
  **AC:** Every deny form, case variant, missing-identity case, direct pre-call denial, and broad post-call denial has a zero-disclosure assertion.
- Search results MUST NOT be persisted or cached. User/conversation joins MUST use normalized metadata caches with user TTL 24 hours and conversation TTL 15 minutes; missing, expired, corrupt, incomplete, or wrong-version entries MUST trigger the shared complete refresh, with no stale fallback. `refresh` is not a public search argument; the resolver/cache seam decides refresh based on freshness and completeness. Cache writes contain metadata only, never query/results/message text.
  **AC:** Repeated identical search calls each make one RTS call; cache spies show metadata-only refreshes and no result/message writes.
- HTTP 429 or a rate-limit envelope MUST immediately raise `SlackRateLimitError` containing only `assistant.search.context` and a parsed non-negative integer `retry_after_seconds: int | None` from the shared Retry-After parser. The shared parser is authoritative: it accepts the project-supported Retry-After forms, returns an integer when valid, and returns `None` for missing, malformed, non-finite, or negative values; search MUST NOT parse or guess this value independently. It MUST not wait, retry, paginate, or fall back. HTTP/auth/provider failures, malformed success envelopes, and transport failures MUST map respectively to shared sanitized provider/auth/response/transport errors; raw status bodies, headers other than safe retry timing, URLs with secrets, tokens, and policy details MUST not escape.

#### Canonical error precedence and record disposition

- Precedence is fixed: (1) local validation; (2) invalid/disabled policy snapshot; (3) direct-target validation/resolution/deny; (4) deadline before provider work; (5) missing capability/credential authentication; (6) transport/status mapping, with recognized 429 taking precedence over generic provider/transport errors; (7) top-level envelope validation; (8) individual-record disposition. No lower-priority check may mask a higher-priority error, and no RTS call occurs for steps 1–5.
- The record table is exact: missing/non-array `results`, non-object envelope, non-boolean/missing `ok`, `ok:false`, malformed `context_messages` container, or a provider response that cannot be parsed is an operation failure (`SlackResponseError`, except recognized auth/429 categories). An individual result/context object with unknown type, missing required field, invalid timestamp/ID/identity, contradictory identity, unsafe content, or failed policy identity is skipped/dropped; it does not fail the operation. A malformed context item is skipped while its valid siblings and primary remain. A malformed primary never contributes context. An empty valid `results` array is success `[]`.
  **AC:** 429, invalid retry headers, `ok:false`, malformed success, timeout, and transport fixtures assert one provider call, typed errors, and no sensitive text.
- The function MUST be registered through the existing async `@tool` mechanism with `namespace="slack"`, `exec_enabled=True`, `exec_docs=False`, and `native=False`. Imports MUST remain lazy enough that sessions without Slack credentials can start. Slack skill documentation MUST describe the exact contract and limitations, including message-only default, explicit files, scopes/reauth, schema, bounds, policy, one request/no pagination, limits, and errors.
  **AC:** Registration and documentation tests assert the exact public surface and unrelated startup remains functional.

## Technical Design

### Overview and ownership

M1 owns the exact shared seams and their contracts: `OperationContext`, authoritative policy shape/enforcement, `policy.py` flat deny evaluation, `resolver.py` canonical conversation resolution, `cache.py` partitioned metadata caches and refresh, `models.py` normalized identity/conversation and canonical `Message`, credentials/scope validation, `client.py` async transport and shared Retry-After parser, sanitized errors, and registration. Search consumes these seams; it does not define replacements, depend on later slices, or expose raw provider records. Within those seams, `search.py` owns validation, direct-target authorization ordering, request construction, result normalization/projection, policy filtering, ordering, deduplication, and bounds; the client owns HTTPS transport, user-token injection, exact endpoint/payload, response/envelope validation, scope/capability checks, and provider-to-sanitized-error mapping.

### Exact code locations and registration

- `agent/src/archie_agent/exec/tools/slack/search.py`: public `search`, validators, `build_rts_request`, filtering/formatting, and fixed-size result projection.
- `agent/src/archie_agent/exec/tools/slack/client.py`: `search_context(payload, *, required_scopes)`; one JSON POST, token boundary, status/error mapping, no retry/cursor.
- `agent/src/archie_agent/exec/tools/slack/models.py`: validated provider records, `SearchHit`, `ContextMessage`, normalized identity, and safe projection types.
- `agent/src/archie_agent/exec/tools/slack/policy.py`: immutable read snapshot, flat deny matcher, and direct pre-call authorization.
- `agent/src/archie_agent/exec/tools/slack/resolver.py`: `resolve_conversation_reference` and cache-only/bulk identity joins; direct resolver is an injectable seam in tests.
- `agent/src/archie_agent/exec/tools/slack/cache.py`: shared metadata cache TTL/completeness/atomic refresh behavior; no search cache.
- `agent/src/archie_agent/exec/tools/slack/__init__.py`: import `search` to trigger the sole `@tool(namespace="slack", native=False, exec_enabled=True, exec_docs=False)` registration; do not register aliases.
- `agent/src/archie_agent/exec/tools/__init__.py`: extend `get_all_tools()` to import the Slack package, preserving existing tools.
- `tests/test_slack_search.py`: offline deterministic unit/service tests with fake client, resolver, policy, clock, cache, and transport seams.
- `persona/skills/slack/SKILL.md`: model-facing seven-function Slack guidance, including this exact search contract.

### Data flow and transport

1. Validate and trim arguments; parse precise UTC bounds and capture an immutable policy snapshot.
2. Resolve and authorize an explicit target before credentials/client access; determine required granular scopes.
3. Load only complete metadata needed for broad filtering via shared cache seams; build the logical payload. `build_rts_request` is tested independently and omits absent optional fields.
4. `client.search_context` performs exactly one `POST` to the fixed HTTPS endpoint with JSON payload and the existing user token in the transport header.
5. Validate the top-level success envelope; malformed individual records are omitted, malformed envelope is an error.
6. Normalize identity, timestamps, text, links, and context; apply exclusive bounds and broad deny filtering to both primaries and context. Context is considered only after its primary survives validation, identity, time, and policy checks, then inherits only under the explicit attachment rule.
7. Deduplicate, order, enforce serialized/collection bounds, apply the requested limit, and return only the fixed projection.

### Error and cache behavior

No retry library, cursor store, result cache, stale metadata fallback, or enrichment request is permitted. A cache refresh is transactional and metadata-only; a failed/incomplete refresh is a typed error. Provider error classes must preserve whether failure was validation, policy, reauthorization/missing scope, authentication, not-found, rate-limit, malformed response, or transport, while exposing only safe method/scope/retry fields.

### Deterministic testing

Tests MUST inject a fixed clock, fake transport, fake credential boundary, resolver, policy snapshot, metadata cache, and user/conversation records. They MUST assert call order (`validate → policy/resolver → cache as needed → RTS → format/filter`), exact logical payload and transport endpoint, one RTS call, no cursor/retry/fallback, scope sets and reauth behavior, all mappings, identity inheritance, byte/collection limits, UTC precision, broad/direct deny behavior, cache refresh rules, sanitized errors, registration, and documentation. No test may use Slack network access or persist message/search data.

## Milestones

### M1.1 — Shared RTS capability and safe public boundary

#### Approach

M1 establishes and owns the complete shared Slack foundation and the first vertical `slack.search` slice. It defines the operation context, authoritative policy, exact resolver/cache/client/error seams, canonical models, credential/scope boundary, and registration, then implements the RTS adapter contract plus strict validator/request builder and the search result path. M1 is not a foundation-only phase and has no dependency on any later Slack slice.

#### Wiring

Create one immutable M1 `OperationContext` at service entry and pass the same context and 60-second monotonic deadline unchanged through validation, policy, direct resolver, metadata cache, capability validation, canonical model normalization, and the one `assistant.search.context` call. Connect the existing user credential boundary and async client; connect `search` to M1’s authoritative policy, resolver, cache, and canonical `Message` seams; import the Slack package from `get_all_tools()`; remove/replace legacy search registration while keeping imports lazy. Search results and message content are never cached; M1 owns metadata-cache lifecycle and refresh contracts, but no later foundation or later slice is required.

#### Edge Cases

Missing/expired token, missing individual scopes, malformed query/options, boolean-as-integer limit, invalid timestamps, reversed bounds, unknown target, direct deny, and unavailable Slack credentials fail before RTS where locally decidable. Reauthorization never swaps the stored token.

#### Tasks

- Define exact signature, validators, UTC precision conversion, defaults, scope matrix, and safe error classes.
- Implement exact logical payload and HTTPS transport boundary with header-only token use.
- Add injectable resolver/policy/cache/client seams and direct-target pre-call authorization.
- Register `slack.search` and remove legacy aliases.

#### Deliverable

A callable `slack.search` that builds the exact single RTS request, checks granular capability, and enforces direct-target policy before provider access.

#### Verify

Run `uv run pytest tests/test_slack_search.py -q -k 'registration or validation or request or scope or reauth or direct or policy'`; assert exact payload, endpoint/header separation, default arrays, call order, zero calls on denial, and one RTS call on valid input.

### M1.2 — Normalize, filter, and bound results

#### Approach

Build on and consume M1’s exact RTS boundary, canonical scope/reauthorization behavior, operation context/deadline, direct-target identity, and shared policy/cache interfaces. Implement envelope validation, exact message/file/channel/user mappings, context normalization, identity inheritance, local policy/time filtering, deterministic ordering, deduplication, and byte/collection bounds; do not replace M1 seams or add a result cache.

#### Wiring

Connect formatter joins to the M1 operation context, shared metadata caches, and flat deny matcher; keep refresh metadata-only and bulk, partitioned by the foundation cache identity; return only the fixed JSON schema from `search`. M2 owns no cache lifecycle, persistence, refresh policy, or search/result cache.

#### Edge Cases

Missing/conflicting identities, malformed hits, denied primary/context entries, cross-conversation context, duplicates, unsafe links, oversized fields, sub-second/equality bounds, empty/all-denied results, timestamp ties, and provider-order mismatches.

#### Tasks

- Implement safe projections and per-type/context mapping with explicit identity inheritance rules.
- Enforce text/ID/name/collection/serialized-size bounds, precise UTC filtering, and safe links.
- Implement relevance preservation, timestamp descending/ID tie-break, post-filter limit, and deduplication.
- Prove no raw blocks/envelopes/sensitive identities or result/message cache writes escape.

#### Deliverable

Bounded model-visible search results that never include denied conversations or context and conform exactly to the safe schema and serialized-size cap.

#### Verify

Run `uv run pytest tests/test_slack_search.py -q -k 'mapping or context or identity or policy or time or order or dedupe or bounds or cache'`; assert all four result types, inheritance/discard rules, byte limits, deterministic output, and no per-result metadata calls.

### M1.3 — Documentation, regression coverage, and integration hardening

M3 consumes the M1 foundation and M2 formatter; it owns documentation and integration verification only, not credentials, scope configuration, cache lifecycle, or later search caches.

#### Approach

Document the finalized model contract and exercise provider, registration, cache, policy, scope/reauth, error, and startup seams as one integrated capability.

#### Wiring

Update `persona/skills/slack/SKILL.md`, package exports, configuration/scope documentation, and focused tests to match the seven-function public surface and shared metadata cache behavior.

#### Edge Cases

429 with valid/invalid `Retry-After`, auth/configuration denial, missing scope reauth, transport failure, malformed envelope, RTS unavailability, cancellation/deadline, all-denied output, cache refresh failure, and sessions without Slack credentials.

#### Tasks

- Add exact documentation for signature, defaults, scopes/reauth, content/channel types, schema, bounds, context, policy, cache, one-request/no-pagination behavior, and errors.
- Add regression tests proving no legacy API, retry, cursor, fallback, raw payload, or search/message cache.
- Run focused Ruff and the relevant agent/package suites; verify only intended files change during implementation.

#### Deliverable

An implementor-ready and documented `slack.search` capability validated against the project’s locked decisions and shared Slack conventions.

#### Verify

Run `uv run pytest tests/test_slack_search.py`, `uv run ruff check agent/src tests/test_slack_search.py`, and the applicable full agent suite; mocked transport must assert one RTS call, permitted metadata calls only, no legacy call, and sanitized errors.