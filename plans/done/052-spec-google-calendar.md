# 052 — Workflow plan: Google Calendar read tools

## Objective

Expose exactly `calendar.today`, `calendar.upcoming`, `calendar.event`, and `calendar.search`, implemented by Python `today()`, `upcoming(days=7, limit=50)`, `event(event_id)`, and `search(start, end, query=None, limit=50)`. Calendar search supports historical and future title/description queries. No Calendar writes.

## Context

These five plans share one Google credential, registration, normalization, error, and documentation contract. The repository root is `/workspace/archie-nexus`; implementation is under `agent/src/archie_agent/exec/tools/`, tests are under `tests/`, and the shared skill is exactly `persona/skills/google/SKILL.md`.

Google access control has only these top-level runtime flags:

```yaml
tools:
  google:
    read:
      enabled: true
    write:
      enabled: false
```

Missing `read.enabled` defaults to `true`; missing `write.enabled` defaults to `false`. Read tools require `tools.google.read.enabled`. Gmail label/archive writes require `tools.google.write.enabled`. There are no Nexus tool-policy scopes, resource scopes, service blocks, operation allowlists, or scope requirements. OAuth scopes remain provider configuration/manual setup and are not Nexus tool-policy scopes.

Every public function uses `@tool(namespace=..., exec_enabled=True, exec_docs=False, native=False)`, is imported explicitly by `get_all_tools()` in `agent/src/archie_agent/exec/tools/__init__.py`, and has no alias. Private adapters and cross-service seams are undecorated and injectable. No implementation exposes tokens, raw provider payloads, arbitrary HTTP, or unbounded content.

Manual OAuth setup (provider configuration, not Nexus tool policy) uses this combined list:

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/meetings.space.readonly`
- `https://www.googleapis.com/auth/drive.meet.readonly`

Operators enable the APIs, configure this list as appropriate, reauthenticate, and start a new session. `OAuthCredential` cannot locally verify granted OAuth scopes; a provider 403 is authoritative.

## Requirements

- All Calendar tools MUST require `tools.google.read.enabled`; missing read defaults true.
  - AC: Disabled calls make zero credential/provider calls and omitted read permits the read tools.
- Inputs MUST be validated before credentials or providers and outputs MUST be fixed, bounded, and sanitized.
  - AC: Tests cover minimum, maximum, just-outside, malformed, timezone, and provider-error cases.
- Calendar and Meet/discovery coupling MUST use private seams, never public-tool lookup.
  - AC: Tests fail if a sibling resolves a public Calendar tool internally.

## Scope and credentials

The work covers Calendar registration, read gating, provider adapters, fixed envelopes, the private event-search seam, tests, and the shared skill section. There are no Calendar writes or policy scopes. OAuth setup is separate manual provider configuration; a provider 403 is authoritative.

## Technical Design

### Policy and credential boundary

050 documents only the two top-level runtime flags and no longer owns a Google scope helper. Calendar checks `tools.google.read.enabled` and does not parse resource, service, or operation policy.

### Calendar contract and private seam

Validate `days` as a non-boolean integer 1–31, list/search `limit` as 1–50, query as 1–500 characters, and event IDs as non-empty control-free strings of at most 256 characters. Fixed event/list/search/detail envelopes are identical and contain only approved bounded fields: event ID, summary, description, start, end, status, organizer, bounded attendees, location, nullable safe Meet URL, and provenance. Today uses the resolved timezone and calendar-day boundaries; upcoming uses deterministic now and the requested 1–31-day window. Search accepts timezone-aware ISO-8601 start/end bounds and searches provider title/description fields. All-day events intersect windows.

The exact private seam is `async def search_events(query, start, end, limit, policy_snapshot)`. It validates query at 1–500 characters, timezone-aware bounds with start < end, and limit 1–50. It uses the primary calendar and the immutable top-level-flag snapshot, returning bounded summaries. Unsafe or redirected Meet URLs are not emitted.

### Registration and loader

Decorate only the three exact functions, add an explicit Calendar import to `get_all_tools()`, and append Calendar guidance to `persona/skills/google/SKILL.md`.

## Milestones
### M1 — Exact registration and windows
**Approach**: Register three exact names, implement bounds, timezone/window rules, read gating, and detail access.

**Public test seam**: Inject flag, clock, timezone resolver, credential/client, and fake transport; assert exact calls and zero denied calls.

**Wiring**: Add the Calendar package to `get_all_tools()` explicitly and append Calendar guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: DST, all-day intersections, missing timezone, 31-day and 50-result boundaries, 403, and missing event.

**Tasks**: Implement validators, time calculations, fixed fields, envelopes, decorators, and loader import.

**Deliverable**: Three correctly keyed Calendar read tools with deterministic windows.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M2 — Private event-search seam
**Approach**: Implement exact `search_events(query,start,end,limit,policy_snapshot)` normalization, pagination, and sibling correlation.

**Public test seam**: Call the undecorated seam with fake clients and flags; assert primary-calendar restriction, exact fields, and no public lookup.

**Wiring**: Add the Calendar package to `get_all_tools()` explicitly and append Calendar guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Empty query, invalid range, malformed times, duplicates, unsafe URLs, and provider errors.

**Tasks**: Add fixed-field adapter, normalization, URL sanitization, and seam tests.

**Deliverable**: A stable private Calendar correlation seam consumed by sibling plans.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M3 — Hardening and documentation
**Approach**: Harden retries, deadlines, redaction, immutable snapshots, and cross-plan flag documentation.

**Public test seam**: Injected clock and transport capture retry/timeout behavior; documentation tests inspect exact paths and defaults.

**Wiring**: Add the Calendar package to `get_all_tools()` explicitly and append Calendar guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: 401 reload, 429/503, timeout, invalid organizer data, DST regression, and secrets.

**Tasks**: Add boundary tests, sanitized errors, skill/loader checks, and cross-plan audit.

**Deliverable**: A tested Calendar package with exact public and private contracts.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

## Acceptance criteria

- Exact public functions and namespace keys are registered once through explicit `get_all_tools()` wiring.
  - AC: Registry tests reject aliases, missing imports, duplicate decorators, and wrong decorator flags.
- Runtime access uses only `tools.google.read.enabled` and `tools.google.write.enabled`, with read default true and write default false.
  - AC: Cross-plan tests prove read gating, Gmail-only write gating, and zero provider calls when denied.
- OAuth scopes are documented as manual provider configuration, not Nexus policy; `OAuthCredential` does not locally verify grants and provider 403 remains authoritative.
  - AC: Documentation and error tests verify the combined OAuth list and authorization classification.
- Outputs are bounded, normalized, deterministic, and free of raw provider data or secrets.
  - AC: Adversarial fixtures cover exact bounds, malformed records, redaction, canonical IDs, and safe state/error distinctions.
- The shared skill exists at `persona/skills/google/SKILL.md`, private seams remain undecorated, and all milestone verification commands pass.
  - AC: Repository-root tests inspect the skill, loader, private seams, and complete public surface.