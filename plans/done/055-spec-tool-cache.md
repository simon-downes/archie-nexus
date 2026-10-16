# 055 — Spec: file-based internal tool cache

## Objective

Add a small file-based cache utility for agent tools that need bounded, infrequently changing metadata. The cache exposes only internal `get(key)`, `set(key, value, ttl)`, `exists(key)`, `valid(key)`, and `del(key)` operations to tool implementation code; it is not registered as an exec tool and is not callable through `exec()`.

Use the cache for Gmail label metadata so user-provided label names are resolved to Gmail label IDs without calling `users/me/labels` on every mutation.

## Context

The Slack tools contain a tool-specific metadata cache under `agent/src/archie_agent/exec/tools/slack/cache.py`. Its JSON envelope, expiry handling, corrupt-cache-as-miss behavior, and atomic replacement are useful patterns, but its namespace and record shape are Slack-specific.

The Gmail implementation currently validates label names and IDs in one combined set, then sends the caller’s value directly to Gmail. Gmail mutations require label IDs, so user-label names must be resolved through the label catalog before constructing `addLabelIds` and `removeLabelIds`.

The cache is intended for normalized, non-secret metadata such as Gmail labels. It is not a general persistence store, does not replace provider truth for mutable resources, and does not expose raw provider responses.

## Requirements

### Cache API and visibility

- The implementation MUST provide an internal cache object/module with:
  - `get(key)`: return the cached `data` value when the key exists and has not expired; return `None` for missing or expired entries.
  - `set(key, value, ttl)`: store `value` with an expiry calculated as current UTC time plus `ttl` seconds.
  - `exists(key)`: return `True` when the key’s cache file exists, regardless of expiry or envelope validity; return `False` when the file is absent or filesystem-unreadable. It MUST validate the key before checking the filesystem.
  - `valid(key)`: return `True` only when a valid cache entry exists and its expiry is more than 10 seconds after the current time; return `False` for missing, malformed, expired, or near-expiry entries.
  - `del(key)`: delete the cache file when present; missing or filesystem-unreadable files are a successful no-op.
- Cache code MUST live in the agent tool implementation layer and MUST NOT use `@tool` or be imported into the public tool registry.
- No cache operation, cache path, or cache file contents may be exposed through a public exec function.
- Cache keys MUST be non-empty safe strings in `<tool>.<item>` form. Each segment MUST be 1–64 ASCII characters matching `[A-Za-z0-9_-]+`; the full key MUST be at most 129 characters and MUST map to `<key>.json` without path traversal or nested-path behavior.
- `ttl` MUST be a finite positive number of seconds. Invalid keys, values, or TTLs MUST be rejected before filesystem access.
- Cached values MUST be JSON objects (`dict`); raw credentials, access tokens, arbitrary provider payloads, and unbounded content MUST NOT be cached.
- Cache values MUST be limited to 256 KiB serialized JSON, with no additional nesting/depth restriction beyond valid JSON object data.

### Storage and envelope

- The cache root MUST be derived from `archie_shared.config.home_dir()` and stored under `<ARCHIE_HOME_DIR>/cache/` (default `~/.nexus/cache/`).
- Each key MUST be stored as one file named `<tool>.<item>.json` directly under the cache root.
- The JSON file MUST contain exactly the cache envelope shape:

  ```json
  {
    "expires": "2026-09-24T12:00:00+00:00",
    "data": {}
  }
  ```

- `expires` MUST be an ISO-8601 UTC timestamp with an explicit timezone offset.
- `get()` MUST treat missing files, malformed JSON, invalid envelope fields, invalid expiry timestamps, expired entries, and non-dict `data` as cache misses.
- `set()` MUST create the cache directory as needed and replace files atomically through a temporary file in the same directory.
- Cache writes SHOULD use restrictive file permissions where supported and MUST not leave temporary files as the normal successful outcome.
- Cache filesystem failures MUST degrade to a miss/no-op rather than preventing the underlying read tool from operating; validation errors remain hard failures.

### Gmail label integration

- `mail.list_labels()` MUST return normalized human-facing `{name, type}` records and use the cache for the normalized label catalog. Gmail IDs MAY be retained only in private implementation data and MUST NOT appear in the public result.
- The cache envelope MUST keep `data` as an object; Gmail MUST store the label list under `data: {"labels": [{"id": "...", "name": "..."}]}`.
- The Gmail label cache key MUST be `google.labels`.
- The initial cache TTL MUST be 24 hours.
- A cache miss or expired entry MUST perform the existing Gmail labels provider request, normalize the result, cache it, and return it.
- Gmail label IDs MUST remain internal to the tool implementation; public consumers MUST use human label names only.
- The Gmail consumer MUST accept and mutate USER labels only through the mutation surface defined by plan 056. It MUST reject system-label names and IDs before any mutation request.
- System-label state changes are separate dedicated operations with human-facing inputs:
  - `mail.archive(message_id)` removes `INBOX`.
  - `mail.unarchive(message_id)` adds `INBOX`.
  - `mail.mark_read(message_id)` removes `UNREAD`.
  - `mail.mark_unread(message_id)` adds `UNREAD`.
  - `mail.star(message_id)` adds `STARRED`.
  - `mail.unstar(message_id)` removes `STARRED`.
- `IMPORTANT`, `SPAM`, `TRASH`, `SENT`, `DRAFT`, and category-system labels are out of scope for dedicated operations and MUST be rejected by the generic USER-label mutation defined by plan 056.
- User-label names MUST resolve to exact Gmail label IDs internally through the cached catalog. Unknown, sensitive, malformed, duplicate, or ambiguous values MUST be rejected before the mutation request.
- Mutation requests MUST contain only internally resolved Gmail label IDs in `addLabelIds` and `removeLabelIds`; IDs MUST never be required or exposed in the public tool contract.
- Message label changes and archive operations MUST NOT invalidate the label catalog because they do not change the account’s label definitions.
- A provider response indicating an unknown/deleted label MAY invalidate `google.labels` and refresh the catalog, but MUST NOT blindly retry the mutation without revalidation and explicit certainty about the failed request.
- `list_labels()` MAY provide an internal refresh path for deliberate cache bypass, but refresh controls MUST not be added to the public exact surface unless separately specified.

## Technical Design

### Module and dependency boundary

Create a generic cache module under `agent/src/archie_agent/exec/tools/` (for example `cache.py`). It may import `home_dir()` from `archie_shared.config`, but it MUST remain undecorated and must not be imported by `get_all_tools()` as a public tool.

The cache should expose a small class or module-level API with dependency-injectable root and clock seams for deterministic tests. Production callers use the Archie home directory and UTC wall-clock time. Tests use a temporary root and a fixed clock; no real home-directory files are touched.

### Validation and serialization

Validate the key before deriving the path. Split the key into exactly two non-empty safe segments and reject controls, separators, `..`, excessive length, and unsupported characters. Serialize only dictionaries with standard JSON; cache normalization is the caller’s responsibility.

Parse expiry using `datetime.fromisoformat`, require timezone awareness, compare against UTC, and treat equal-to-now as expired. Do not use file mtime as cache truth.

Write to a same-directory temporary file, flush/close it, then atomically replace the destination. A failed write must not delete a previously valid cache entry.

### Gmail resolver

Keep the provider request and normalization in `mail.py`. Add an internal cached loader that returns normalized labels and optionally refreshes. Build separate lookup maps by label ID and exact label name. Resolve system aliases to canonical Gmail IDs and user names to IDs before calculating current/add/remove deltas.

The cache stores only the normalized list under `data`; it does not store OAuth state, raw Gmail payloads, message content, or provider errors.

## Milestones

### M1 — Internal cache utility

**Approach**: Implement the generic file cache with injectable root/clock, strict key/TTL/envelope validation, UTC ISO expiry, cache-miss semantics, atomic writes, and non-fatal filesystem handling.

**Test seam**: Instantiate the cache with a temporary directory and fake clock; exercise `get`/`set` without loading the public tool registry.

**Edge Cases**:

- Missing file → `get()` returns `None`.
- Expired timestamp → `get()` returns `None`.
- Equal-to-now timestamp → `get()` returns `None`.
- Malformed JSON/envelope/expiry/data → `get()` returns `None`.
- Invalid key or TTL → typed validation error before filesystem access.
- Cache directory absent → `set()` creates it.
- Write failure → existing entry remains intact and tool-level operation can continue.
- Temporary replacement interrupted → previous destination is not removed by the cache implementation.

**Tasks**:

1. Add the internal cache module and production root resolution.
2. Add key, TTL, envelope, and JSON validation.
3. Add injectable clock/root seams.
4. Implement atomic replacement and best-effort filesystem failure behavior.
5. Add tests proving no public `cache.*` exec registration.

**Deliverable**: A reusable internal `get`/`set` file cache with the exact JSON envelope and deterministic tests.

**Verify**:

```bash
uv run pytest tests/test_tool_cache.py -q
uv run ruff check agent/src/archie_agent/exec/tools/cache.py tests/test_tool_cache.py
```

### M2 — Gmail label catalog caching and ID resolution

**Approach**: Make the normalized Gmail label catalog cache-backed with key `google.labels` and a 24-hour TTL. Resolve user names to exact Gmail IDs before the batch mutation defined by plan 056, while preserving the existing write-gating and public read semantics.

**Wiring**: `mail.list_labels()` and the private mutation resolver use the cache. Cache misses call `gmail_request("labels", ...)`; successful normalization populates the cache. Public tool names and signatures remain unchanged.

**Edge Cases**:

- Warm cache → no labels provider request.
- Missing/expired cache → one labels request followed by cache population.
- Input is an existing label ID → use it directly.
- Input is an exact user-label name → translate to its ID.
- Input is an unknown name/ID → reject before `modify`.
- Duplicate names/IDs or add/remove overlap → reject or normalize deterministically before `modify`.
- System aliases → map to canonical Gmail label IDs without querying user labels unnecessarily.
- Archive of an already archived message → no mutation and no unnecessary label-catalog lookup.
- Label catalog provider failure → preserve the typed provider error and do not mutate.

**Tasks**:

1. Add cached normalized-label loading to `mail.py`.
2. Define USER versus SYSTEM label normalization and canonical system-label mappings.
3. Build exact human-name-to-ID resolution and deterministic add/remove arrays without exposing IDs.
4. Ensure system labels and unknown user labels fail before the Gmail modify request.
5. Add the dedicated public operations `mail.unarchive`, `mail.mark_read`, `mail.mark_unread`, `mail.star`, and `mail.unstar` with human-facing inputs.
6. Preserve the existing public cache/Gmail read operations; the Gmail mutation surface follows plan 056 and remains restricted to USER labels.
7. Add tests for warm/miss/expired cache, name resolution, system-label rejection, dedicated operations, unknown labels, and no-op archive.

**Deliverable**: Gmail mutations send only resolved Gmail label IDs and use a reusable 24-hour cached label catalog.

**Verify**:

```bash
uv run pytest tests/test_google_mail.py tests/test_tool_cache.py -q
uv run ruff check agent/src/archie_agent/exec/tools/mail.py tests/test_google_mail.py
```

### M3 — Contract audit and hardening

**Approach**: Audit cache visibility, file contents, path safety, serialization bounds, Gmail mutation ordering, and failure behavior across the agent tool loader.

**Test seam**: Inspect registered tool keys and temporary cache files; use fake clocks and provider spies to prove cache behavior without exposing cache operations publicly.

**Edge Cases**:

- Cache file contains tokens/raw payloads → caller normalization prevents storing it; cache accepts only dict shape, not arbitrary provider envelopes.
- Key traversal/control characters → rejected before path construction.
- Cache file from a future timestamp → treated as valid until expiry.
- Corrupt cache → provider refresh, not an exception from the public tool.
- Cache write failure → provider result still returns.
- Provider label order changes → resolved mutation arrays remain deterministic.

**Tasks**:

1. Add cross-tool registration and visibility assertions.
2. Add adversarial key/path/envelope tests.
3. Add Gmail provider-call ordering and payload assertions.
4. Document the internal cache boundary and Gmail cache behavior in relevant code comments/docs.
5. Run the complete Google test suite and scoped repository checks.

**Deliverable**: A documented, internally scoped cache utility with correct Gmail label ID resolution and no public cache surface.

**Verify**:

```bash
uv run pytest tests/test_google_*.py tests/test_tool_cache.py -q
uv run ruff check agent/src tests/test_google_*.py tests/test_tool_cache.py
```

## Acceptance Criteria

- `cache.get(key)` returns cached dict data only while unexpired; missing, expired, malformed, or unreadable entries return `None`.
- `cache.exists(key)` distinguishes a present cache file from a missing/unreadable file without considering expiry.
- `cache.valid(key)` returns `False` when the entry expires within the next 10 seconds.
- `cache.del(key)` removes a key as an idempotent no-op when absent.
- `cache.set(key, value, ttl)` writes the exact two-field JSON envelope with an ISO-8601 UTC expiry.
- Keys map only to safe flat `<tool>.<item>.json` filenames under `<ARCHIE_HOME_DIR>/cache/`.
- Cache operations are unavailable through `exec()` and absent from `get_all_tools()`.
- Cache writes are atomic and cache filesystem failures do not break the underlying tool read path.
- Gmail label names resolve to IDs before mutation, system aliases remain supported, unknown labels are rejected, and mutation payloads contain IDs only.
- `google.labels` uses a 24-hour cache and cache miss/expiry refreshes from Gmail.
- Existing public Google tool surfaces remain unchanged.
- Tests cover cache lifecycle, corruption, expiry, path safety, Gmail name/ID resolution, and provider-call ordering.
