# 048 — Spec: `slack.send_message`

## Objective

Implement `slack.send_message` as the explicit, policy-gated Slack write in Archie’s replacement seven-function surface. It sends caller-provided text to one existing, permitted conversation and optionally posts the message as a reply in an existing thread. It MUST never create, join, open, or discover a new conversation, claim success for an uncertain mutation, retry a possibly transmitted request, or expose raw Slack data.

The implementation is complete only when the public contract, write boundary, exact result projection, tests, registration, and Slack skill documentation agree.

## Context

Project `042-project-slack-personal-assistant.md` defines an internal Slack app acting as the authenticated user. Its locked decisions require `slack.send_message` to use an existing permitted conversation, an independent write policy and flat `write.deny` list, an optional thread timestamp, immediate rate-limit errors, and no automatic retry or stale fallback. The replacement surface has no compatibility aliases; old provider-shaped Slack functions are removed rather than wrapped.

The adjacent Slack specs own the shared conversation identity, metadata, policy, error, credential, and registration seams. This spec owns the mutation request, argument/result contract, mutation outcome classification, and replacement of any old message-sending surface. Message content is not cached. The project does **not** authorize an automatic Archie prefix: the requested `text` is sent unchanged.

There is currently no Slack implementation, focused Slack test module, or Slack skill to preserve in this repository. Implementors MUST follow the existing async exec-tool architecture, typed credential store, immutable session policy snapshot, and service namespace conventions rather than introducing a Slack-specific credential path or raw provider tool.

## RFC2119 Requirements and acceptance criteria

Every requirement below is normative. Each requirement is observable through the acceptance criteria or milestone verification.

### Public API and exact result schema

1. The only public send function MUST have exactly this signature:

   ```python
   async def send_message(
       conversation: str,
       text: str,
       *,
       thread_ts: str | None = None,
   ) -> dict
   ```

   No token, cursor, blocks, attachments, provider client, retry option, prefix option, or arbitrary Slack API arguments may be exposed.

2. `conversation` MUST be a non-empty, trimmed string of at most 200 Unicode characters; surrounding whitespace is rejected, not silently sent or used to create a second spelling. The shared resolver MUST accept exactly these forms and no others: (a) a canonical Slack conversation ID matching `\A[CGD][A-Z0-9]+\Z`; (b) `#` followed by an exact channel name; or (c) an exact one-to-one DM participant username, real name, display name, or authorized email. Group-DM participant references, arbitrary names, wildcards, partial names, and send-only aliases are invalid. The resolver contract is `resolve_existing(reference: str) -> ResolvedConversation | ResolutionError`, where `ResolvedConversation` contains exactly `conversation_id`, `kind`, `name`, `participant_aliases`, `archived`, `user_participates`, and `required_read_scopes: frozenset[str]`; it returns one canonical, non-archived, participating conversation plus the read scopes required for metadata resolution, or a sanitized invalid/unresolved/ambiguous/access error. Resolution MUST select exactly one existing conversation in which the authenticated user participates.

3. `text` MUST be a string after type validation, MUST be trimmed only for validation, and MUST contain at least 1 and at most 40,000 Unicode characters. The original caller text, including intentional leading/trailing whitespace, MUST be sent unchanged. Empty or whitespace-only text, non-strings, booleans, and over-limit text MUST fail locally. No automatic Archie prefix or other text is permitted.

4. `thread_ts`, when supplied, MUST be a non-empty string matching exactly `\A[0-9]{1,20}\.[0-9]{6}\Z` (one-to-20-digit whole-seconds component, a literal period, and exactly six fractional digits; maximum length 27; no surrounding whitespace). `None` means a top-level message. The validated string MUST be preserved byte-for-byte in the logical request and result; it MUST NOT be rounded, reserialized, or replaced with a parent timestamp. Booleans, non-strings, empty values, malformed timestamps, and over-limit values MUST fail locally.

5. On confirmed provider success, the function MUST return a JSON-serializable dictionary with **exactly** these keys (nullable values are present as `None`, never omitted):

   ```python
   {
       "conversation_id": str,
       "message_ts": str,
       "thread_ts": str | None,
       "permalink": str | None,
   }
   ```

   `conversation_id` is the canonical Slack conversation ID. `message_ts` is the provider-created message timestamp in Slack timestamp form. `thread_ts` is the effective thread timestamp: the supplied valid `thread_ts` for a reply, or `None` for a top-level message. `permalink` is a validated provider permalink when the success response supplies one; otherwise it is `None`. No raw response, text, blocks, author, channel object, request ID, or provider metadata may be returned.

6. A success response is valid only when the top-level JSON object has `ok: true`, contains a non-empty canonical conversation ID matching the authorized target, and contains a valid message timestamp matching `\A[0-9]{1,20}\.[0-9]{6}\Z`. A supplied permalink is retained only when it is an absolute HTTPS URL with a non-empty host and no userinfo, query, or fragment; an absent permalink becomes `None`. Missing, malformed, or contradictory required fields MUST be treated as an uncertain mutation outcome, not as success. The logical success envelope is mapped from Slack’s `channel`, `ts`, and optional `permalink` fields only; extra fields are ignored.

### Write gate, target policy, and ordering

7. The function MUST obtain the immutable Slack write-policy snapshot from the invocation context and MUST reject a disabled write surface before credentials, metadata-cache refresh, resolver/provider access, or the mutation client. A read-enabled/write-disabled session MUST still reject this function. Exact ordering is mandatory: local validation → immutable policy snapshot and disabled-write gate → target reference classification → obtain the credential → resolver resolves the target and returns its required read scopes → validate the union of resolver-required read scopes plus `chat:write` → deny evaluation → mutation. The authoritative policy shape is exactly `{ "enabled": bool, "read": { "enabled": bool, "deny": list[str] }, "write": { "enabled": bool, "deny": list[str] } }`; no missing keys, extra keys, aliases, or alternative nesting is accepted. The write gate is independent of `read.deny` and read enablement.

8. The flat `write.deny` list MUST be validated and resolved with exact, case-insensitive matching using the project rules: `#name` matches a channel name, `C...`/`G...`/`D...` matches a conversation ID, and other entries match one-to-one DM participant username, real name, display name, or authorized email. Group DMs MUST be denied by conversation ID, never by participant lists. Matching MUST NOT be fuzzy, substring-based, prefix-based, or inferred from a hidden target; an identity alias resolving to zero or multiple eligible one-to-one DMs is invalid and fails closed.

9. The policy seam MUST validate the authoritative shape from requirement 7, with both `read.deny` and `write.deny` flat lists of strings and no aliases, wildcards, nested rules, or fuzzy expressions. A malformed policy MUST fail closed with a sanitized policy error before target classification. Deny entries, hidden target existence, participant identities, and omitted counts MUST not be disclosed. Deny-reference resolution MUST occur only after the target resolver has returned the canonical target and its required read scopes.

10. The target MUST be resolved to one existing canonical conversation and MUST be checked against the flat write denylist before `chat.postMessage`. Resolution MUST use the complete, non-archived shared conversation metadata seam; archived records are invalid even if returned by a refresh, and missing/expired/corrupt/incomplete metadata MUST use the documented non-mutating refresh or fail rather than stale-fallback. An invalid, unresolved, ambiguous, archived, or denied target MUST make zero `chat.postMessage` calls. A target that is readable but write-denied MUST be rejected. Resolution MUST NOT open, create, join, invite to, or otherwise change conversation membership.

11. The function MUST send only to an existing, non-archived conversation in the authenticated user’s permitted Slack context and in which that user participates. It MUST use the existing typed user-token credential seam and require `chat:write` plus every read scope declared by the resolver for metadata resolution; no resolver read scope authorizes the mutation itself. Missing credentials or any required scope MUST fail before transmission. The resolver MAY use its declared read scopes only for metadata resolution; this does not violate the write operation’s independent write authorization. It MUST NOT use `conversations.open`, `conversations.create`, `conversations.join`, `chat.postEphemeral`, webhooks, incoming URLs, or any fallback provider method. The mutation provider call MUST be exactly Slack `chat.postMessage`.

12. If target resolution needs metadata, it MAY use the shared complete normalized conversation metadata cache and its documented non-mutating refresh seam, but it MUST not call `chat.postMessage` until canonical resolution and write-deny evaluation succeed. No message content or mutation result may be cached.

### Provider mapping and mutation outcome

13. The logical payload for a top-level call MUST contain exactly `{"channel": <canonical conversation ID>, "text": <exact caller text>}`. A reply payload MUST contain exactly those two fields plus `"thread_ts": <validated supplied timestamp>`; the field is omitted, never null, for a top-level call. The transport MUST transmit that logical payload as the JSON body of exactly one Slack Web API `chat.postMessage` request using the existing authenticated user-token client boundary. The adapter MUST NOT add an Archie prefix, alter text, send blocks, attachments, metadata, unfurl options, form fields, or silently change a reply into a top-level message.

14. The adapter MUST use one bounded request and MUST NOT automatically retry. This prohibition includes SDK retries, transport retries, timeout retries, retry-after sleeps, and application-level retries after a request may have been transmitted.

15. A response with HTTP success but Slack `ok: false`, or an explicit Slack rejection, authentication/authorization error, invalid-target error, or other confirmed non-mutation provider response MUST raise its distinct sanitized Slack error and MUST never be reported as success. The error MUST not include tokens, raw response bodies, secret-bearing URLs, message text, or hidden policy details.

16. HTTP 429 or a Slack rate-limit envelope MUST immediately raise `SlackRateLimitError` containing only method `chat.postMessage` and the shared `Retry-After` value of type `int | None`; the shared parser’s missing or invalid value is represented as `None` and MUST NOT be guessed. The function MUST not sleep, retry, invoke another provider method, or return a success-shaped value.

17. The shared transport contract MUST define `TransmissionState = Literal["not_started", "started", "completed"]` and `TransmissionResult` with exactly `state: TransmissionState` plus either a received `response` or a sanitized `error`; every attempt, including exceptions and cancellation, MUST return or raise with that state preserved. `not_started` means no request handoff began; `started` means handoff may have begun and no response was received; `completed` means a response was received. A failure known to be `not_started` MAY use a sanitized transport/configuration error. A timeout, connection loss, cancellation after `started`, or any exception whose state cannot be proven `not_started` MUST raise `SlackMutationIndeterminateError`; it MUST not retry and MUST not return a result. A malformed success envelope, missing required success fields, or contradictory response after `completed` is also indeterminate because the mutation may have succeeded.

18. Error precedence MUST be deterministic: local validation and disabled/invalid policy errors precede all provider work; a recognized HTTP 429 or Slack rate-limit envelope takes precedence over generic provider/transport mapping; an explicit, fully received non-success envelope is a confirmed provider error; otherwise any started/unknown transmission, malformed envelope, or incomplete/contradictory success is mutation-indeterminate. The implementation MUST NOT perform a read-after-write verification request to manufacture certainty. If the single `chat.postMessage` response does not provide required confirmation, the outcome remains indeterminate.

### Replacement, safety, and documentation

19. The replacement public name MUST be `slack.send_message`. Any old send-message function, registration, alias, or provider-shaped public surface MUST be removed, not retained as a compatibility wrapper. Only the project-approved seven Slack functions may remain publicly registered.

20. The implementation MUST keep raw provider payloads, credentials, request bodies containing message text, and message content out of logs, caches, sanitized errors, and model-visible diagnostics. The result MUST not be persisted.

21. The Slack skill and focused documentation MUST state the exact signature, exact result schema, unchanged-text behavior, 40,000-character bound, existing-target-only rule, optional `thread_ts`, write gate and denylist ordering, `chat.postMessage` mapping, immediate 429 behavior, no-retry rule, and indeterminate outcome. It MUST not document credentials, raw API calls for model use, or an Archie prefix.

### Acceptance criteria

- **AC1:** Signature inspection and public invocation prove the exact three-argument contract; invalid conversation/text/thread values make no provider mutation call.
- **AC2:** A disabled write policy fails before credentials and provider access; read-enabled/write-disabled is rejected; malformed/unresolved write-deny configuration fails closed.
- **AC3:** A permitted existing channel, DM, and group-DM target resolves to its canonical ID; archived, unknown, invalid, read-only, or write-denied targets make zero `chat.postMessage` calls and do not create/join/open anything.
- **AC4:** Tests prove exact case-insensitive flat deny semantics for `#name`, `C/G/D` IDs, and one-to-one participant identities; group-DM participant names do not deny a group DM; no fuzzy or substring match occurs.
- **AC5:** Recorded top-level requests contain exactly the canonical `channel` and unchanged `text`; recorded reply requests additionally contain the supplied `thread_ts`; no blocks, prefix, alternate method, or text normalization is sent.
- **AC6:** Valid text at lengths 1 and 40,000 succeeds through the fake provider; empty/whitespace-only, 40,001-character, non-string, boolean, and malformed timestamp inputs fail locally.
- **AC7:** A valid `ok: true` success with matching `channel`/`ts` returns exactly the four documented keys, canonical IDs, the exact effective thread timestamp, and an HTTPS/no-userinfo-query-fragment permalink or `None`; absent/invalid permalink is `None`, and extra provider fields never escape. Missing/contradictory `ok`, `channel`, or `ts` is indeterminate.
- **AC8:** Credential fixtures prove missing credentials and missing `chat:write` fail before transmission with the shared sanitized configuration/reauthorization error. Explicit provider rejection is a sanitized non-success error; HTTP 429 fails immediately with method and valid retry detail when present, and no sleep/retry/second call.
- **AC9:** Timeout, post-transmission transport failure, unknown transmission state, malformed success, missing IDs/timestamps, and contradictory success fixtures raise mutation-indeterminate, make no retry, and never return success; a proven pre-transmission failure retains its distinct sanitized transport/configuration category.
- **AC10:** The old send registration is absent, `slack.send_message` is present in the replacement namespace, and tests and skill docs describe only the final contract with no automatic Archie prefix.

## Technical Design

### Ownership and seams

Implement a thin async Slack mutation adapter and a public send service. The service owns argument validation, immutable write-policy lookup, denylist validation, target resolution, authorization ordering, and exact result projection. The adapter owns `chat.postMessage` request construction, one-attempt transport invocation, response validation, and provider-to-sanitized-error mapping. The provider client MUST not own denylist logic or model-facing formatting.

Use the shared invocation context for the immutable policy snapshot, typed credential/scope validator, conversation resolver, metadata cache, deadline, transmission-state transport, and common Slack errors. The central Slack registry is owned by the shared Slack foundation’s existing `agent/src/archie_agent/exec/tools/slack/__init__.py` registration hook (the sole import/allowlist owner); this spec MUST supply the `send_message` import through that hook, and the foundation’s exact-seven allowlist MUST be the sole public exposure check. No local registry, second namespace, or alternate import owner is permitted. The concrete shared client seam MUST be `async chat_post_message(payload: Mapping[str, object], *, required_scopes: frozenset[str], deadline: MonotonicDeadline) -> ProviderResponse`; `ProviderResponse` MUST carry either the validated envelope or a sanitized typed failure plus the shared `TransmissionState`. The service MUST obtain the resolver’s required read-scope set from target classification/resolution, then validate the union of those scopes with `frozenset({"chat:write"})` against the existing credential grant before `chat.postMessage`; missing credentials or any required scope raises the shared sanitized authentication/configuration/reauthorization error before transmission. Resolver read scopes are used only for metadata resolution and do not authorize the write. The mutation adapter MUST receive a canonical conversation ID and validated values; it MUST not resolve names, consult policy, or select alternate methods. No requirement may imply that `chat:write` alone is the full credential scope set.

### Suggested components and locations

- `agent/src/archie_agent/exec/tools/slack/send_message.py`: public function, local validation, policy/target orchestration, and exact projection.
- `agent/src/archie_agent/exec/tools/slack/client.py`: shared async Slack boundary; add only `chat.postMessage`, exact logical-payload mapping, `chat:write` scope validation, transmission-state reporting, one-attempt behavior, response validation, and immediate 429 mapping.
- `agent/src/archie_agent/exec/tools/slack/models.py`: validated timestamp/request and `SendMessageResult`; keep provider envelopes separate from public dictionaries.
- `agent/src/archie_agent/exec/tools/slack/policy.py` and conversation-resolution seam: reuse shared write gate, exact flat write denylist, canonical target resolution, and archived/participation checks.
- `agent/src/archie_agent/exec/tools/slack/__init__.py`: shared foundation registration hook and sole Slack namespace/allowlist owner; import `send_message` here with `namespace="slack"`, `exec_enabled=True`, `exec_docs=False`, and `native=False`, and remove obsolete send aliases. This spec MUST NOT create another registry or namespace.
- `persona/skills/slack/SKILL.md`: create or replace the seven-function guidance with this exact send contract.
- `tests/test_slack_send_message.py`: the concrete public test seam. Each test invokes the registered `slack.send_message` through a `FakeSlackInvocationContext` exposing `policy_snapshot`, `credential_store`, `conversation_resolver`, `metadata_cache`, `deadline`, and `chat_post_message`; its `FakeSlackClient` records `calls`, exact payload/method/scope/deadline, and `TransmissionState`. Tests MUST inject this context through the project’s established invocation-context fixture and use no network dependency.

Module names may vary if the same ownership and seams remain explicit. No unrelated provider or generic credential architecture may be changed for convenience.

### Request flow

1. Validate `conversation`, `text`, and `thread_ts` without provider access; preserve the original text.
2. Read and validate the immutable policy snapshot, then reject a disabled write surface before any other work.
3. Classify the target reference form locally (canonical ID, exact channel reference, or exact one-to-one participant identity) without resolving it or accessing provider metadata.
4. Obtain the typed authenticated-user credential without yet authorizing or transmitting the mutation.
5. Invoke `resolve_existing`; it resolves the classified reference and returns one canonical, non-archived, participating conversation plus its required read-scope set. Resolver read scopes may be used only for metadata resolution.
6. Validate the union `resolver_required_read_scopes | frozenset({"chat:write"})` against the credential grant. `chat:write` is the independent write authorization; resolver read scopes do not authorize the write.
7. Evaluate the exact flat `write.deny` policy against the resolved target; reject all invalid/access/ambiguous/denied targets. No mutation call is possible until this succeeds.
8. Build exactly one `chat.postMessage` request with canonical `channel`, unchanged `text`, and optional `thread_ts`.
9. Classify the single response: 429/rate-limit first, then explicit fully received rejection, then confirmed success; all malformed, incomplete, contradictory, started, or unknown outcomes are mutation-indeterminate.
10. Return the result without caching or logging content. No retry or verification call is allowed.

### Bounds and error mapping

Use named constants for conversation length (200), text length (40,000), thread timestamp length (27), and thread timestamp pattern (`^[0-9]{1,20}\.[0-9]{6}$`). Use a monotonic operation deadline supplied by the shared Slack context for metadata refresh and the single mutation attempt. Keep the public result small and fixed. Preserve distinct sanitized categories for policy, validation, configuration/authentication/scope, provider authorization/not-found, rate limit, transport, response, and mutation-indeterminate failures. Error serialization includes type and safe message only.

A fake provider MUST record the exact logical payload, method, call count, required scope set, deadline use, and transmission state (`not_started`, `started`, or `completed`) so tests can distinguish pre-transmission failure from uncertain post-transmission failure. The production transport MUST expose the same state on success and exception, set its SDK/HTTP retry configuration to zero/disabled (including connect, read, status, and redirect retries), and MUST have no application retry loop, backoff, sleep, retry-after wait, or read-after-write verification.

## Milestones

### M6.1 — Shared mutation seam and exact `chat.postMessage` adapter (prefactor)

**Dependencies:** M1-M5

**Approach:** Prefactor the existing shared Slack foundation’s mutation seam, adding the typed send request/result/error seams and one-attempt `chat.postMessage` mapping. Do not expose the adapter as a public tool.

**Wiring:** `send_message` will call the shared `client.chat_post_message(payload, *, required_scopes=resolver_required_read_scopes | frozenset({"chat:write"}), deadline)` with one exact logical payload; the adapter receives only validated canonical values and returns a confirmed provider response or a typed failure carrying transmission state. The shared credential seam, not this slice, owns token storage, scope grants, refresh, and reauthorization.

**Edge cases:** exact text preservation; optional thread field; malformed envelope; missing conversation ID/timestamp; invalid/absent permalink; explicit rejection; HTTP 429; pre-transmission versus post-transmission transport failure; unknown transmission state; no SDK/application retry or retry configuration.

**Tasks:**
1. Define bounded request and exact four-field result models.
2. Implement `chat.postMessage` argument mapping with no prefix, blocks, fallback, or alternate method.
3. Implement exact `ok`/channel/ts/permalink response validation, sanitized response/error classification, deterministic error precedence, and immediate rate-limit handling.
4. Add adapter tests for exact request shape, `chat:write` scope, 429, explicit errors, malformed success, permalink rules, and each transmission state.

**Deliverable:** One prefactored, one-attempt adapter seam that returns only a validated provider result or sanitized typed failure.

**Verify:** `uv run pytest tests/test_slack_send_message.py -q -k 'adapter or provider or rate or scope or transport'` and assert the exact method/logical payload, `chat:write` validation, fake call count `1`, retry configuration disabled, transmission states, no sleep, and exact error precedence/types.

### M6.2 — Write-gated existing-target send service

**Approach:** Implement the exact public signature and all local validation, then wire the immutable write gate, flat denylist preflight, shared complete existing-target resolver, archived/access/participation checks, canonical ID, credential scope validation, and pre-mutation ordering.

**Wiring:** Local validation → immutable policy snapshot/shape and disabled-write gate → target reference classification → obtain credential → `resolve_existing` returns the canonical target and required read scopes → validate the union of resolver-required read scopes plus `frozenset({"chat:write"})` → exact write-deny evaluation → one adapter call → exact result projection. Resolver read scopes are used only for metadata resolution; resolution may consume only the shared normalized metadata/cache seam and must never mutate Slack. No credential or cache refresh occurs before the disabled-write decision.

**Edge cases:** read allowed/write denied; disabled write before credentials/cache/provider; malformed or unresolved deny entries; channel/DM/group-DM matching; denied target; archived, inaccessible, non-participating, ambiguous, or unknown target; missing credential/scope; valid top-level message; valid thread reply; exact timestamp preservation; whitespace-preserving text; absent/invalid permalink; empty result is not applicable because sends return one result or error.

**Tasks:**
1. Implement validation constants, exact timestamp grammar/preservation, and target-form validation.
2. Wire policy, credential/scope, metadata resolver, and denylist ordering so disabled/denied/inaccessible targets make zero mutation calls.
3. Implement exact `ok`/canonical ID/message ts projection and HTTPS permalink validation.
4. Add public tests for AC1–AC8 using fakes and assert call ordering, no cache writes/content logs, and credential/scope behavior.

**Deliverable:** One public `slack.send_message` service that safely sends one permitted message or reply with the exact result/error contract.

**Verify:** `uv run pytest tests/test_slack_send_message.py -q -k 'validation or policy or target or payload or result or permalink or scope'` and then `uv run pytest tests/test_slack_send_message.py -q`; assert call order, exact payload omission/preservation, archived/access behavior, all conversation forms, denylist semantics, no cache/content persistence, and exact four-key projection.

### M6.3 — Replacement registration, docs, and regression hardening

**Approach:** Use the project foundation’s central seven-function registry and shared Slack skill ownership: register only the replacement function, remove old send aliases, publish the final send guidance, and test the complete mutation safety boundary. This slice MUST NOT create a second registration, credential store, OAuth configuration, or Slack skill root.

**Wiring:** The central Slack namespace exposes `send_message` with exactly the established metadata `namespace="slack"`, `exec_enabled=True`, `exec_docs=False`, and `native=False`; the other six approved Slack functions remain intact. No native/raw provider registration, duplicate namespace, compatibility alias, or second registry is added.

**Edge cases:** duplicate registration; old-name import/registry lookup; documentation drift; missing `chat:write`/reauthorization; 429/no retry; post-transmission uncertainty; unknown transmission state; no automatic Archie prefix; provider payload leakage.

**Tasks:**
1. Add/remove registrations and compatibility references.
2. Update the project-owned `persona/skills/slack/SKILL.md` and focused documentation with exact examples and limitations; do not create a competing skill path. Documentation MUST include `chat:write` capability/reauthorization behavior without exposing credential values or raw provider-call instructions.
3. Add registration and documentation regression tests, including explicit unchanged-text/prefix assertions.
4. Run formatting, type checks, focused tests, and the relevant repository test suite.

**Deliverable:** One central replacement registration plus one project-owned documentation update exposing the implementor- and model-ready send contract with no obsolete surface.

**Verify:** `uv run pytest tests/test_slack_send_message.py tests/test_skills.py -q`; run the repository’s configured formatter/type checks; inspect the central registry to confirm exactly the seven project-approved Slack functions, `slack.send_message` metadata, absence of every old send alias/provider-shaped registration, no duplicate namespace, and no credential-path or skill-path duplicate. Assert docs contain the exact signature/schema, `chat:write`, thread grammar/preservation, target/access rules, immediate 429/no-retry, and indeterminate semantics without an Archie prefix.

## Final completion checklist

- [ ] Exact signature and four-key result schema are implemented and tested.
- [ ] Write gate and flat write denylist run before mutation-provider access.
- [ ] Only existing permitted conversations are accepted; no open/join/create behavior exists.
- [ ] Text is bounded and sent unchanged; there is no automatic Archie prefix.
- [ ] `thread_ts` matches the exact grammar, is preserved unchanged, and maps only to `chat.postMessage` replies.
- [ ] `chat:write` is validated through the shared credential seam; no second credential path exists.
- [ ] 429 is immediate and explicit; no retry configuration, retry, sleep, fallback, or verification exists.
- [ ] Transport exposes transmission state and deterministic precedence maps every uncertain outcome to indeterminate, never success, and never retry.
- [ ] Old surface is removed; central registration, project-owned skill, tests, and docs are replaced together.
