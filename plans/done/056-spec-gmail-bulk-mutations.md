# 056 — Spec: Gmail bulk mutations

## Objective

Replace the single-message Gmail mutation surface with bounded bulk mutations that use Gmail's `users.messages.batchModify` endpoint. Callers provide a list of message IDs with a maximum of 500 IDs per invocation; callers are responsible for batching larger result sets. Rename `mail.modify_labels` to `mail.label`.

The read surface remains unchanged: `mail.search` continues to return bounded message projections and `mail.read` remains single-message.

## Context

The current Gmail tools perform one `messages.modify` request per message and expose `mail.modify_labels`. Common workflows search for multiple messages and then apply the same label or system-state change to all results. Gmail provides `users.messages.batchModify`, which accepts up to 1,000 IDs, shared add/remove label IDs, and returns an empty successful response.

The caller, rather than the tool, will own batching. Archie deliberately uses a stricter 500-ID limit to keep request size, validation, and mutation scope bounded. Each tool invocation makes one provider mutation request, so there is no in-tool partial-batch result contract. A cache miss for USER-label resolution may add one Gmail label-catalog read before that single mutation request; the one-request invariant applies to the mutation operation itself.

This spec supersedes the Gmail mutation portions of plan 051 and the Gmail consumer portions of plan 055. Where those plans say that Gmail mutations are single-message, prohibit bulk mutation, require per-message pre-reads, expose `mail.modify_labels`, or permit system-label mapping through the generic label operation, plan 056 is authoritative. Plan 051 remains authoritative for Gmail reads, credentials, policy defaults, normalization, and unsupported send/reply/delete surfaces. Plan 055 remains authoritative for the generic cache API and storage contract, except where this spec defines Gmail's consumer behavior.

## Requirements

- Public mutation tools MUST accept `message_ids: list[str]` with 1–500 IDs.
- `add` and `remove` label-name lists MUST each contain 0–50 values, with at least one total value for `mail.label`; each value is a non-empty control-free string of at most 256 characters. Values MUST be duplicate-free within each list, and a value MUST NOT appear in both lists after USER-label resolution. The normalized batch JSON body MUST be at most 64 KiB before transmission.
- IDs MUST be validated before policy, credential, or provider access; IDs must be non-empty, control-free, at most 256 characters, and duplicate-free.
- The exact public mutation surface MUST be:
  - `mail.label(message_ids, add=None, remove=None)`
  - `mail.archive(message_ids)`
  - `mail.unarchive(message_ids)`
  - `mail.mark_read(message_ids)`
  - `mail.mark_unread(message_ids)`
  - `mail.star(message_ids)`
  - `mail.unstar(message_ids)`
- `mail.modify_labels` MUST be removed from the public surface and tool registry.
- `mail.search`, `mail.read`, and `mail.list_labels` remain unchanged except for documentation and registry assertions affected by the rename.
- Gmail mutations MUST use one `POST users/me/messages/batchModify` request per invocation with JSON `{ids, addLabelIds, removeLabelIds}`.
- The tool MUST never split a list larger than 500; it MUST reject it before provider access. Callers are responsible for batching.
- USER label names remain the only labels accepted by `mail.label`; Gmail label IDs remain internal and are resolved from the cached catalog.
- System labels MUST be managed only through their dedicated operations. `mail.label` MUST reject system-label names and IDs.
- Dedicated operations MUST map to one shared system label mutation:
  - `archive`: remove `INBOX`
  - `unarchive`: add `INBOX`
  - `mark_read`: remove `UNREAD`
  - `mark_unread`: add `UNREAD`
  - `star`: add `STARRED`
  - `unstar`: remove `STARRED`
- Dedicated operations MUST not require a per-message pre-read. Gmail batchModify is the authoritative single request; already-applied state is an idempotent provider operation.
- Writes MUST require the Google write policy. For `mail.label`, the tool MUST validate the cached catalog first; read access is required only immediately before a cache-miss USER-label catalog provider call. Dedicated system operations do not require Google read access.
- Invalid message IDs, label inputs, or list sizes MUST be rejected before policy evaluation, cache access, credential access, or provider access. Tests MUST spy on policy and credential boundaries, not only provider calls.
- Successful mutation tools MUST return a bounded aggregate result, not raw provider payloads. The result MUST include the submitted count and completion, for example:

  ```json
  {"count": 3, "complete": true}
  ```

- Provider errors MUST retain the existing sanitized typed-error behavior. The tool MUST not automatically retry an uncertain mutation. The existing Google skill's explicit-confirmation rule MUST be updated for bulk targets: before mutation, the caller must confirm the operation and the exact bounded ID list it selected (or a user-visible message list/count whose IDs were deterministically obtained); after an uncertain response, the tool reports the sanitized transport error and does not retry or claim completion.
- No message content, OAuth tokens, raw provider responses, or arbitrary HTTP may be exposed.

## Technical Design

### Gmail request adapter

Extend the private Gmail adapter with a `batch_modify` operation that posts to. Add an `allow_empty` response option at the shared JSON transport boundary, defaulting to `False` for existing operations and set to `True` only for `batch_modify`; this preserves current JSON validation elsewhere while allowing Gmail's documented empty successful response.

```text
https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify
```

The request body contains only resolved IDs and label IDs. Gmail's successful `batchModify` response is accepted as an empty body, `None`, or an empty JSON object; any non-empty unexpected response is a sanitized `GoogleTransportError`. The adapter normalizes accepted success responses to an internal success value; public tools return the bounded aggregate result.

### Validation and resolution

Add a shared private validator for `message_ids`. It rejects non-lists, booleans/non-string members, empty/control-containing IDs, duplicates, and lists outside 1–500 items before policy or credential access.

`mail.label` accepts human label names only; callers MUST NOT provide Gmail IDs. The implementation performs syntactic input validation before policy/cache/credential/provider access. It then loads and validates the cached normalized catalog first. On a valid warm cache, it may proceed without Google read access; on a cache miss or malformed cache, it requires read access immediately before fetching the catalog. Catalog-dependent unknown-name and SYSTEM-label rejection occurs after that catalog load and before the batch mutation request. It resolves USER names to internal IDs, rejects duplicates and add/remove overlap, then issues one batch request.

Dedicated system operations bypass label-catalog resolution and issue one batch request with their fixed internal system ID. They do not expose the internal ID in output.

### Registration and documentation

Update `mail.__all__`, registry tests, and the Google skill to remove `mail.modify_labels` and document `mail.label` plus list-based dedicated mutations. Update plan 051's stale single-message/bulk-exclusion language to point to this spec.

## Milestones

### M1 — Batch adapter and bounded public mutation surface

**Approach**: Add `batch_modify`, shared 1–500 ID validation, aggregate success envelopes, and the renamed `mail.label` surface. Preserve existing write gating and sanitized errors.

**Test seam**: Inject `gmail_request`, policy, credential, and cache boundaries and assert exactly one `batch_modify` mutation call, exact JSON body, validation-before-policy/cache/credential/provider behavior, and aggregate output.

**Wiring**: `mail.py` owns validation, label resolution, batch payload construction, and aggregate output. `gmail_request` owns the single `batch_modify` HTTP operation. `get_all_tools()` discovers the renamed decorated function through the existing mail import; the skill and tests consume the resulting exact public keys.

**Edge Cases**:

- Non-list IDs → typed validation error with zero calls.
- Empty, duplicate, malformed, or 501 IDs → typed validation error with zero calls.
- 500 IDs → one provider request.
- Successful empty Gmail response → `{count: N, complete: true}`.
- Provider failure → typed error, no retry.

**Tasks**:

1. Add batch request adapter.
2. Add message-ID list validator.
3. Rename `modify_labels` to `label`.
4. Resolve USER label names and build ID-only batch payloads.
5. Add aggregate mutation result normalization.
6. Update registry and public-surface tests, including removal of the old decorator/key and the exact `mail.label(message_ids, ...)` signature.

**Deliverable**: `mail.label` accepts 1–500 message IDs and performs one Gmail batch mutation.

**Verify**:

```bash
uv run pytest tests/test_google_mail.py -q
uv run ruff check agent/src/archie_agent/exec/tools/mail.py tests/test_google_mail.py
```

### M2 — Dedicated bulk system operations

**Approach**: Convert archive/unarchive/read/unread/star/unstar to list-based operations using the shared batch mutation seam. Keep system IDs private and avoid catalog reads.

**Test seam**: Provider spies assert one request per operation, fixed label direction, no label-catalog call, and correct 1–500 validation.

**Wiring**: Each dedicated public function validates the shared ID list, checks the write gate, and calls the shared private batch helper with one fixed internal system label ID. No function reads messages or the label catalog.

**Edge Cases**:

- Already-applied state → one idempotent batch request, no pre-read.
- Read policy disabled but write enabled → dedicated system mutation remains allowed.
- Write policy disabled → zero provider calls.
- 500 IDs → one request; 501 → validation error.

**Tasks**:

1. Convert dedicated operation signatures to `list[str]` and update their exact registry entries.
2. Route all operations through one internal batch helper.
3. Preserve exact operation-to-label mappings.
4. Add tests for all dedicated operations and policy ordering.

**Deliverable**: All public Gmail mutations support bounded caller-batched lists without per-message requests.

**Verify**:

```bash
uv run pytest tests/test_google_mail.py tests/test_google_*.py -q
uv run ruff check agent/src tests
```

### M3 — Contract audit and documentation

**Approach**: Audit the public registry, skill, plan 051 references, ID privacy, aggregate envelopes, and no-retry mutation behavior.

**Test seam**: Inspect `get_all_tools()`, skill text, exact request payloads, and full Google test results.

**Tasks**:

1. Remove all public and documentation references to `mail.modify_labels`.
2. Update plan 051 and plan 055 Gmail consumer sections so they no longer contradict this spec; preserve their unrelated read/cache contracts.
3. Document the 500-ID caller batching contract.
4. Assert no Gmail IDs leak through public mutation outputs or label listings.
5. Add malformed provider/empty-response tests.
6. Run full Google tests and scoped lint.

**Deliverable**: A documented, tested bulk Gmail mutation surface with one provider request per invocation.

**Verify**:

```bash
uv run pytest tests/test_google_*.py tests/test_tool_cache.py -q
uv run ruff check agent/src tests/test_google_*.py tests/test_tool_cache.py
```

## Acceptance Criteria

- The public key `mail.modify_labels` is absent and `mail.label` is present.
- Every mutation accepts a validated list of 1–500 message IDs.
- Each invocation makes exactly one Gmail `batchModify` request.
- Caller-provided lists larger than 500 are rejected; the tool never performs implicit batching.
- USER label names resolve internally to IDs; system labels are unavailable through `mail.label`.
- Dedicated system operations use fixed private mappings and do not query the label catalog.
- Mutation outputs contain only bounded aggregate status/count data.
- No mutation is automatically retried after an uncertain provider response.
- Search/read behavior remains compatible apart from callers passing search IDs to bulk mutations.
- Tests cover validation, request payloads, one-call behavior, all dedicated operations, policy gating, renamed registration, and documentation.
