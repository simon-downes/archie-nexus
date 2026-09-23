# 049 — Spec: `slack.react`

## Objective

Implement `slack.react` as Archie’s explicit, user-visible Slack mutation for adding or removing one emoji reaction on one known message. The operation MUST resolve and authorize the conversation and exact message before the mutation request, use the independent Slack write gate and flat write denylist, expose a stable result rather than a Slack response, and never claim success when the mutation outcome is uncertain.

## Context

This is milestone M7 of [`042-project-slack-personal-assistant.md`](042-project-slack-personal-assistant.md), the final capability in the replacement seven-function Slack surface. That project requires a fresh public surface with no old aliases, one authenticated user-token integration, independent read/write policy, flat deny lists, direct-target authorization before mutation, immediate rate-limit errors, no automatic retries, and no message-content cache. `slack.send_message` (Spec 048) establishes the neighboring mutation and shared outcome conventions.

The shared Slack foundation (M1-M6) owns the operation context, central registration, typed credential path, OAuth scope validation, client boundary, policy snapshot, normalized models, resolver interfaces, and sanitized error taxonomy. M1-M6 own conversation and known-message resolution. This slice MUST consume those seams and MUST NOT create a second registry, credential store, policy format, or resolver syntax. This spec owns the reaction operation, its request mapping, result projection, and focused tests/documentation.

A reaction is not context retrieval. Reaction state MUST NOT be read to manufacture idempotency, persisted, or cached. Slack’s `reactions.add` and `reactions.remove` are the only mutation operations in scope. Removing an absent reaction, reacting to an inaccessible/unreactable message, or receiving an explicit Slack rejection is an error, not a successful no-op.

## RFC2119 Requirements and acceptance criteria

Every requirement below is normative. Each has an acceptance criterion in this section or is covered by milestone verification.

### 1. Public signature and exact result

- The only public reaction function MUST be:

  ```python
  async def react(
      conversation: str,
      timestamp: str,
      reaction: str,
      *,
      action: Literal["add", "remove"] = "add",
  ) -> dict[str, str]
  ```

- The registered name MUST be `slack.react`. The public function MUST NOT expose a channel parameter, cursor, token, raw payload, provider method/client, retry option, or provider response. `action` MUST default to `"add"`.
- A confirmed success MUST be a JSON-serializable dictionary with exactly these keys and no others:

  ```python
  {
      "conversation": str,  # canonical Slack conversation ID
      "timestamp": str,      # exact validated input spelling
      "reaction": str,       # canonical bare emoji name
      "action": Literal["add", "remove"],
  }
  ```

- `conversation` MUST be the resolved `C...`, `G...`, or `D...` ID, never a display name. `timestamp` MUST be the validated input, unchanged. `reaction` MUST be the canonical bare name. Key order SHOULD be the order shown.
- The result MUST be constructed only after a provider response has been validated as a JSON object with `ok` exactly `True`; `ok` missing, non-boolean, or false is not success. Extra provider fields MUST never escape.

**AC1:** Signature/registration inspection and add/remove fixtures prove the exact four-key result, with no raw response fields.

### 2. Local validation and canonicalization

- All local validation MUST precede credentials, resolver/provider access, and mutation. Failures MUST be sanitized typed Slack validation errors and MUST disclose neither credentials nor deny-list contents.
- `conversation`, `timestamp`, and `reaction` MUST be strings. `conversation` and `reaction` MUST be non-empty; leading/trailing whitespace MUST be rejected, not trimmed for provider use. `conversation` MUST be at most 200 Unicode code points and MUST be passed unchanged to the shared conversation resolver.
- `timestamp` MUST match `^[0-9]{1,12}\.[0-9]{6}$`, be finite and numerically greater than zero, and be no longer than 19 characters. Signs, exponent notation, missing/extra fractional digits, whitespace, zero, negative values, non-strings, and overlong values MUST be rejected. A valid timestamp MUST be sent unchanged; wall-clock age MUST NOT be used for rejection.
- `reaction` MUST be a bare Slack emoji name of 1–50 Unicode code points matching `^[A-Za-z0-9_+\-]+$`. It MUST NOT begin or end with `_`, `+`, or `-`, contain whitespace, control characters, colons, slashes, provider markup, URL syntax, glyphs, or modifier/skin-tone syntax. `thumbsup`, `party-parrot_2`, and `+1` are valid; `:thumbsup:` is not. No emoji catalog lookup or provider normalization is performed.
- `action` MUST be exactly the string `"add"` or `"remove"`; booleans, null, aliases, and all other values MUST be rejected. The validated action selects the provider method and cannot be changed later in the flow.

**AC2:** Tests cover timestamp `1712345678.000001` and `1.000000`, malformed/zero/negative/overlong values, the named emoji boundaries, whitespace/glyph/slash/colon cases, invalid actions, and zero mutation calls for every invalid input.

### 3. Shared scopes, credential ordering, write gate, and target policy

- The project OAuth request/credential validator MUST require `reactions:write` for this operation. The M1 resolver owns the target-type read-scope contract: `C...` channel targets require `channels:read`, `G...` private-channel targets require `groups:read`, `D...` one-to-one DM targets require `im:read`, and `G...` group-DM targets (identified by the normalized conversation type, not by the ID prefix) require `mpim:read`. A name/participant reference that can resolve to more than one target type MUST use the resolver-declared union of those read scopes before metadata access. The credential validator MUST receive exactly `{reactions:write}` unioned with that resolver-declared read set, using the existing authenticated user token; this spec MUST NOT invent a token, scope, or second validator. Missing scopes MUST raise the shared sanitized scope/reauthorization error naming only the missing scope set.
- Ordering MUST be observable and exact: (1) local validation; (2) load the immutable policy snapshot; (3) validate the complete flat policy shape/references and reject disabled writes; (4) classify the target reference; (5) obtain the typed authenticated-user credential; (6) call the M1 exact-message resolver to resolve the conversation and exact message and return the required read scopes; (7) validate the union of those required read scopes and `reactions:write` against the credential; (8) evaluate `write.deny` against the resolved normalized target; (9) invoke one reaction mutation. Policy validation MUST precede credential access; target classification MUST precede credential access; scope validation MUST follow resolver return; resolution MUST precede deny evaluation; deny evaluation MUST precede mutation. Read enablement and `read.deny` MUST neither enable nor disable this operation.
- The M1 exact-message owner/resolver MUST expose `resolve_known_message(conversation_ref: str, timestamp: str, *, deadline: MonotonicDeadline) -> ResolvedKnownMessage`. It receives the validated conversation reference and exact timestamp, returns one normalized message identified canonically by `conversation_id` + `ts`, plus the normalized conversation used for policy and the required target read-scope set. The returned `ts` MUST equal the input byte-for-byte. It MUST verify both canonical conversation identity and exact message identity; mismatch, ambiguity, missing, archived, inaccessible, or non-message results are sanitized resolution errors. It MUST NOT search broadly, select another timestamp, create/join/open a conversation, or mutate membership.
- Message validation MUST use the shared bounded known-message seam: at most one lookup scoped to the canonical conversation and exact `ts`, with a provider page/range bounded to that one timestamp and the operation deadline. It MUST NOT call general search, enumerate unbounded history, or read reactions. A cache may supply only eligible normalized conversation metadata; message content, message records, and reaction state MUST never be cached or used to bypass authorization.
- `write.deny` MUST be an independent flat list of strings, with no nested objects, aliases, wildcards, or inherited rules. Its target model is exactly: `#name` for a channel name; `C...`, `G...`, or `D...` for one exact case-insensitive canonical conversation ID; or, only for a one-to-one `D...` DM, one exact case-insensitive participant username, real name, display name, or authorized email. A group-DM normalized target is matchable only by its canonical conversation ID, never by participant lists. Matching is one-to-one and exact: no fuzzy, substring, wildcard, prefix, case-sensitive, or inferred matching. The shared policy matcher owns validation and evaluation; `react.py` MUST NOT reimplement it.
- Deny entries MUST be validated and resolved fail-closed. An invalid, ambiguous, unresolved, archived, inaccessible, or denied target MUST raise the appropriate sanitized validation/policy/resolution error and MUST make zero reaction mutation calls. Errors MUST NOT reveal matching entries, hidden target existence, participants, or omitted counts.

**AC3:** Ordering spies prove policy precedes credentials, credentials/scopes precede provider use, resolution precedes deny evaluation, and all denied/unknown/archived/unresolved targets make zero `reactions.add`/`reactions.remove` calls. A permitted target reaches exactly one mutation method.

### 4. Exact provider mapping, envelopes, errors, and transmission state

- `action="add"` MUST invoke exactly once the provider method `reactions.add`; `action="remove"` MUST invoke exactly once `reactions.remove`. The request payload MUST be exactly `{"channel": canonical_id, "timestamp": exact_ts, "name": canonical_bare_reaction}`, where `canonical_id` is the resolved `conversation_id` and `exact_ts` is the validated exact `ts`. No colon wrapping, alternate ID, options, or caller/provider fields are permitted.
- The shared M1-M6 adapter MUST expose the existing transmission contract, not a reaction-specific variant: `mutation(method: Literal["reactions.add", "reactions.remove"], payload: Mapping[str, str], *, required_scopes: frozenset[str], deadline: MonotonicDeadline, transmission: TransmissionState) -> ProviderResponse`. `TransmissionState` has exactly the shared states `not_started`, `started`, and `completed`; the transport calls `mark_started()` immediately before handing bytes to the network and never resets it. `may_have_transmitted()` is true for `started` and for an unknown boundary. A proven pre-start exception MAY map to `SlackTransportError`; after start or at an unknowable boundary it MUST map to `SlackMutationIndeterminateError`; cancellation and timeout follow the same rule.
- The bounded shared `ProviderResponse` contract contains only `status: int | None`, `headers: Mapping[str, str]` restricted to rate-limit parsing, `body: Mapping[str, object] | None`, and `transmission: TransmissionState`; it is at most 16 KiB / 32 top-level fields and the reaction adapter accepts only a JSON object with exactly one required field, `ok`, whose value is the boolean `True`. `{"ok": true}` is therefore the complete success envelope; extra fields are rejected as malformed (never projected). `ok: false` is an explicit rejection. Missing/non-boolean `ok`, non-object, oversized, malformed, or contradictory responses are not success and map according to transmission state.
- Response precedence is exact: transport/cancellation/deadline classification first; then HTTP 429 or a recognized rate-limit envelope; then bounded-envelope validation; then `ok is False` provider rejection; only then may `ok is True` produce success. Explicit `ok: false` maps to sanitized `SlackProviderError` (including permission, authentication, missing message, missing reaction, locked/unreactable message, and invalid emoji), regardless of an accompanying provider error string. No raw error string, response body, URL, token, or stack trace may escape.
- HTTP 429 takes precedence over every body interpretation and MUST immediately raise `SlackRateLimitError(method=<attempted method>, retry_after_seconds=<int | None>)`. For non-429 responses, a valid Slack rate-limit envelope takes precedence over generic provider-error mapping and yields the same envelope. HTTP `Retry-After` takes precedence over the JSON `retry_after` value; each MUST be parsed by the shared Retry-After parser, which accepts only a syntactically valid non-negative integer seconds value (surrounding ASCII whitespace allowed) and normalizes it to the shared `int | None` contract. Missing, negative, fractional, decimal, HTTP-date, malformed, and other non-integer values become `None`. The error contains no status, headers, body, or other fields. It MUST not sleep, retry, switch methods, query state, or return success.
- No automatic retry is permitted for any error, including SDK/HTTP retries, timeouts, connection failures, cancellation, malformed envelopes, or unknown exceptions. No read-after-write verification or idempotency lookup is permitted. If a request may have reached Slack, the error MUST explicitly state that the result is unknown, include only the attempted method as operation detail, and MUST NOT claim the reaction was added/removed or recommend retrying.

**AC4:** Fakes assert exact method/payload and one call only; `{ok: true}` succeeds; every explicit rejection and 429 is a sanitized non-success with no sleep; pre-transmission and possibly-transmitted failures map distinctly and never retry.

### 5. Caching, replacement, documentation, and tests

- Reaction state, message content/records, provider envelopes, credentials, and normalized reaction results MUST NOT be cached, persisted, logged, or written to metadata storage. Cache spies MUST show no reaction-state read/write and no cache bypass of resolution or mutation.
- All failures MUST use the shared sanitized taxonomy: validation, write-policy, scope/authentication, resolution/not-found, provider response, transport, rate limit, or mutation-indeterminate. No hidden denylist or participant information may escape.
- The replacement public name MUST be `slack.react`; obsolete reaction functions, aliases, provider-shaped registrations, and compatibility wrappers MUST be removed. Central foundation registration owns the single Slack namespace and exact seven-function allowlist; this slice MUST only supply its function registration hook and MUST NOT register a second namespace.
- Focused offline tests MUST use fake policy, credential/scope, resolver, cache-spy, transmission-state, and provider seams. They MUST cover signature/registration, all validation boundaries, credential ordering and scopes, ID/name/DM/group-DM deny semantics, bounded exact-message lookup, add/remove payloads, canonicalization, exact result keys, every error mapping, Retry-After parsing, no cache/state read, one-attempt behavior, and pre/post-transmission uncertainty. No network or persistence is allowed.
- Shared Slack skill guidance and focused documentation MUST state the exact signature/result, bare-name rule, action mapping, required write gate, deny model, known-target/bounded lookup, no-cache/no-retry behavior, immediate rate limits, and unknown outcomes. It MUST not teach raw API calls or credentials.

**AC5:** Registration has `slack.react` and no obsolete alias; tests and docs describe only the final contract; cache and sanitization assertions pass.

## Technical Design

### Ownership and data flow

The reaction service owns local validation, policy ordering, target-reference classification, credential handoff, flat denylist authorization, and the exact result. M1 owns exact-message resolution; this spec consumes that resolver and its returned read-scope contract. The thin provider adapter owns only exact request construction, selected method, one-attempt transport/transmission state, envelope validation, Retry-After parsing, and provider-to-sanitized-error mapping. The client contains no policy or model-facing formatting.

The call flow MUST be observable and occur in exactly this order:

1. Perform all local validation and reaction canonicalization.
2. Load the immutable policy snapshot, validate its complete shape/references, and apply the disabled-write gate.
3. Classify the target reference locally (ID, channel name, or permitted DM participant reference) without resolving it.
4. Obtain the typed authenticated-user credential.
5. Call the M1 exact-message resolver, which resolves the conversation and exact message identity (`conversation_id` + `ts`) and returns the required target read-scope set.
6. Validate the union of the resolver-returned read scopes and `reactions:write` against the obtained credential.
7. Evaluate the independent flat `write.deny` policy against the resolved normalized conversation.
8. Select exactly one method from the already validated action and invoke it once.
9. Accept only `ok is True` and return the four-key result; otherwise raise the mapped error.

Resolution may use provider metadata calls, but `reactions.add`/`remove` MUST not occur until every gate passes. “No provider call” in this spec means no reaction mutation call; tests MUST record lookup and mutation separately.

### Suggested seams and locations

- `agent/src/archie_agent/exec/tools/slack/react.py`: public `react`, validation, orchestration, and result normalization.
- `agent/src/archie_agent/exec/tools/slack/client.py`: shared async Slack boundary, scope checks, transmission state, `reactions.add`/`reactions.remove`, envelope and rate-limit mapping.
- `agent/src/archie_agent/exec/tools/slack/policy.py`, resolver/model modules: shared policy snapshot, flat denylist, canonical conversation and bounded message resolution; do not duplicate rules in `react.py`.
- `agent/src/archie_agent/exec/tools/slack/__init__.py` and central foundation registry: import the module through the existing hook; do not create a second Slack namespace.
- `tests/test_slack_react.py`: offline tests with fake policy, credentials, resolver, cache spy, transmission state, and provider.
- `persona/skills/slack/SKILL.md` (or the shared replacement location): model-facing contract and safety guidance.

Use the existing `@tool`/`ToolError` pattern. Credentials remain in the typed runtime path. Provider exceptions are translated at the Slack boundary before model-visible output. No retry library, queue, idempotency store, reaction cache, or read-after-write verification is permitted.

### Timestamp and target resolution

The M1 exact-message resolver owns lookup, authorization, normalization, and not-found/error behavior; this slice owns no provider lookup or message model. Its interface is `resolve_known_message(conversation_ref, timestamp, *, deadline)` and its result includes `required_read_scopes`. It first resolves one complete, non-archived normalized conversation, then performs at most one bounded history lookup constrained to that canonical `conversation_id` and exact `ts`. It MUST compare the returned conversation ID to the canonical ID and the normalized message timestamp to the exact input string (not merely a numerically equal timestamp); any mismatch or ambiguity fails closed. It does not read reactions or cache message records. The mutation adapter receives only canonical `conversation_id`, exact `ts`, bare name, the union of required read scopes and `reactions:write`, the deadline, and shared `TransmissionState`.

### Uncertain outcomes

The shared M1-M6 provider boundary MUST mark transmission before transport handoff and conservatively treat an unknown boundary as possibly transmitted. Known pre-transmission failures map to `SlackTransportError`; post-transmission timeout, cancellation, transport failure, malformed success, missing `ok`, and process interruption map to `SlackMutationIndeterminateError`. Explicit provider responses, including 429, are deterministic provider/rate-limit errors. Neither category may retry or perform a state lookup.

## Milestones

### M7.1 — Shared write-gated reaction vertical slice

**Dependency/ownership boundary:** M7 depends on M1-M6, the already-delivered shared Slack foundation contracts (OperationContext/deadline, `TransmissionState`/`ProviderResponse`, typed credential and target-type scope validator, policy snapshot/matcher, normalized conversation/message models, exact-message resolver, sanitized errors, and central registry). This milestone consumes those seams and MUST NOT define replacements. The central registry remains the sole owner of the `slack` namespace and exact seven-function allowlist; M1 of this slice registers no public function and only supplies the callable plus its registration metadata to that owner. Start with one permitted `add` path end to end; keep the provider fake at the shared mutation boundary.

**Wiring:** `react` validates locally, loads and validates the immutable policy before credentials, obtains the typed authenticated-user token, classifies the target reference, calls the M1 exact-message resolver, validates the union of its returned read scopes with `reactions:write`, applies the shared `write.deny` matcher, and passes only canonical `conversation_id`, exact `ts`, bare name, the union of required read scopes and `reactions:write`, deadline, and shared `TransmissionState` to the adapter.

**Edge Cases:** writes disabled or invalid policy → sanitized policy error before credentials; missing scope → sanitized scope error; unresolved/archived/denied target → no reaction mutation; permitted add → one exact `reactions.add` call and four-key result; no reaction cache access.

**Tasks:**
1. Consume the shared M1-M6 credential/scope, policy, exact-message, normalized-model, error, transmission, and adapter seams; define only this slice’s orchestration seam and registration metadata, never a replacement contract.
2. Implement the exact signature, local validation, ordering, bounded target resolution, and denylist check.
3. Implement `reactions.add` request construction, `{ok: true}` validation, and exact normalization.
4. Add tests for registration, ordering, canonical IDs, bounded lookup, no mutation on denial, payload, result schema, and no-cache behavior.

**Deliverable:** A permitted `slack.react(..., action="add")` call performs one correctly mapped mutation and every pre-call denial is observable without a mutation call.

**Verify:** `uv run pytest tests/test_slack_react.py -q -k 'registration or validation or ordering or policy or add or lookup or cache'`; fake records show policy/credential/resolver sequence and one add payload.

### M7.2 — Remove path, emoji contract, and deterministic provider errors

**Approach:** Extend the same service and adapter. Add strict bare-name validation and map `remove` to the separate Slack method; preserve shared scope and error behavior.

**Edge Cases:** colon-wrapped/glyph/malformed/oversize emoji → local validation; absent reaction/unreactable message → explicit sanitized provider error; 429 with valid/invalid Retry-After → immediate typed error and no subsequent call.

**Tasks:**
1. Implement and test the complete timestamp/action/emoji matrix and reaction canonicalization.
2. Implement exact `reactions.remove` mapping and remove success result.
3. Add explicit rejection, malformed envelope, authentication, permission, missing-message, missing-reaction, invalid-emoji, and rate-limit mappings.
4. Test Retry-After parsing, no state lookup, no retry, no sleep, and no cache access on deterministic failures.

**Deliverable:** Add and remove are distinct, fully validated, policy-gated operations with stable success and sanitized deterministic failure contracts.

**Verify:** `uv run pytest tests/test_slack_react.py -q -k 'remove or emoji or provider or rate_limit or retry_after'`; inspect fake calls for exact method/payload and immediate termination.

### M7.3 — Uncertain mutation handling and replacement integration

**Approach:** Consume (do not redefine) the shared M1-M6 `TransmissionState`/`ProviderResponse` contracts and add the one-attempt reaction mapping without retries or idempotency storage. Wire the callable through the central registry owner and remove obsolete aliases only through that owner’s existing replacement seam.

**Edge Cases:** proven pre-transmission failure → transport error; post-transmission timeout/connection failure/cancellation → mutation-indeterminate; malformed/missing/contradictory success → mutation-indeterminate; explicit `ok: false` → provider error; no retry in every case.

**Tasks:**
1. Consume and test the shared `TransmissionState`, bounded `ProviderResponse`, and `SlackMutationIndeterminateError` contracts with conservative boundary classification; do not define reaction-local variants.
2. Ensure cancellation/timeout and malformed-success handling cannot produce success or a second call.
3. Submit only the `slack.react` callable and its established metadata to the foundation registry owner; that owner updates the exact-seven allowlist, rejects duplicates, removes old aliases, and preserves the other six functions. This slice MUST NOT create or mutate a local registry or namespace.
4. Update shared Slack skill/focused docs with exact result, scopes, mapping, gates, bounded lookup, no-cache, rate-limit, and uncertainty behavior.

**Deliverable:** The replacement capability is integrated and cannot falsely report or automatically repeat an uncertain mutation.

**Verify:** `uv run pytest tests/test_slack_react.py -q`; registration/documentation assertions pass; fake transport tests show one call and indeterminate errors for every possibly transmitted failure.

### M7.4 — Full acceptance and repository checks

**Approach:** Run the focused suite, adjacent shared Slack/provider/policy tests, lint, and authoritative repository checks. Fix only contract/integration defects; do not weaken validation or safety rules.

**Tasks:**
1. Run the complete reaction suite and adjacent shared foundation, credential/scope, resolver, provider, and policy tests.
2. Confirm no message/reaction persistence, cache reads for reaction state, or cache writes through spies and repository search.
3. Confirm docs/replacement names and seven-function registration have no obsolete references.
4. Run formatting/lint and the repository’s full test suite.

**Deliverable:** The spec is implemented with complete offline acceptance coverage and no code path bypassing the write gate, flat denylist, bounded known-target resolution, exact mapping, or uncertain-outcome rule.

**Verify:** `uv run pytest tests/test_slack_react.py`; `uv run ruff check .`; `uv run pytest`; review the final diff to confirm no unrelated files changed.

## Acceptance checklist

- [ ] Exact signature, action default, required `reactions:write` scope, and four-key result are implemented.
- [ ] Bare emoji-name, timestamp, action, and whitespace validation is exact; reaction output is canonical bare spelling.
- [ ] Policy is validated before credentials; independent write enablement and flat `write.deny` are evaluated before reaction mutation.
- [ ] Conversation and exact message resolve through the shared bounded seam; no broad search or reaction-state lookup exists.
- [ ] `add` maps only to `reactions.add`; `remove` maps only to `reactions.remove` with exact payloads.
- [ ] Provider envelopes, explicit errors, and Retry-After values map to sanitized immediate errors.
- [ ] Transmission state distinguishes proven pre-send transport failures from possibly transmitted failures.
- [ ] No reaction/message cache, state lookup, retry, sleep, or read-after-write verification exists.
- [ ] Replacement central registration, tests, skill guidance, and docs remove obsolete aliases and cover the final contract.
