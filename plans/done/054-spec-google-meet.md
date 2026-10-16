# 054 — Workflow plan: Google Meet artifact tooling

## Objective

Expose exactly `meet.notes`, `meet.transcript`, and `meet.recording`, implemented by Python `notes`, `transcript`, and `recording`. No free-text Meet search, participant export, mutation, recording download, or auth implementation.

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

- All Meet tools MUST require `tools.google.read.enabled`; missing read defaults true.
  - AC: Disabled calls make zero credential/provider calls and omitted read permits reads.
- Recording output MUST contain links/metadata only, never recording bytes. Notes and transcripts MUST remain bounded.
  - AC: Tests reject bytes and enforce the exact 30,000-character text cap.
- Canonical resolution MUST use private Calendar/Drive seams and canonical IDs; no public sibling lookup is allowed.
  - AC: Resolver tests inspect exact calls, candidate caps, and ambiguity errors.

## Scope and credentials

This plan covers Meet registration, read gating, canonical resolution, artifact adapters, private Calendar/Drive integration, tests, and the shared skill section. It has no write surface, no artifact-download surface, and no policy controlling recording-link inclusion. OAuth setup is separate manual provider configuration; `OAuthCredential` cannot locally verify granted scopes and provider 403 is authoritative.

## Technical Design

### Policy and credential boundary

050 documents the two top-level flags only and no longer owns a Google scope helper. Meet checks the read flag and does not parse resource, service, operation, or artifact policy. Recording links are normalized output references, not bytes and not a tool-policy setting.

### Canonical Meet contract

Public meeting IDs are at most 500 characters. Canonical resolution is exact: `calendar:<event_id>` calls Calendar.get, extracts a strict Meet URL or code, then calls `Meet.conferenceRecords.list` filtered by code and time; ambiguity is rejected. `meet:<conference_record_id>` resolves directly. `drive:<file_id>` validates artifact metadata and correlation. Resolution allows at most 10 candidates, 20 seconds total, and 8 seconds per request.

Meet smartNotes are recognized when available; fallback notes require a Drive file with Google Doc MIME, title/correlation pattern, and source provenance, never a generic title-only file. Successful calls return exactly one state: `available`, `not_ready`, `missing`, or `unavailable`; these are states, not errors. Ambiguity, authorization, validation, configuration, authentication, timeout, and transport failures are sanitized typed errors. Text is capped at exactly 30,000 characters. Recording is link-only and never bytes.

### Registration and loader

Decorate only the three exact functions, add an explicit Meet import to `get_all_tools()`, and append Meet guidance to `persona/skills/google/SKILL.md`. Use private `search_events`, `search_references`, and `read_reference` seams only.

## Milestones
### M1 — Exact registration and resolver
**Approach**: Register exact names, enforce bounds, read gating, canonical resolution, and private Calendar/Drive injection.

**Public test seam**: Inject flag, clock, transport, Calendar.get, `search_events`, `search_references`, and `read_reference`; assert caps and no fallback after direct 403/404.

**Wiring**: Add the Meet package to `get_all_tools()` explicitly and append Meet guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Malformed IDs, invalid URLs/codes, ambiguous matches, 10-candidate/20-second/8-second limits, and authorization.

**Tasks**: Implement validators, canonical resolver, strict extraction, deadline budget, errors, and loader wiring.

**Deliverable**: Three correctly keyed Meet tools with canonical resolution.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M2 — Artifact recognition and envelopes
**Approach**: Implement notes, transcript, and recording retrieval; recognize smartNotes or provenance-backed Docs; map four states and enforce limits.

**Public test seam**: Fakes cover smartNotes, valid/invalid correlated Docs, not-ready, missing, unavailable, links, and oversize text.

**Wiring**: Add the Meet package to `get_all_tools()` explicitly and append Meet guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Generic title-only file, wrong MIME, mismatched correlation, 403/404, secret metadata, bytes, and truncation.

**Tasks**: Add recognizers, MIME/correlation checks, state mapping, link-only normalization, and tests.

**Deliverable**: A bounded Meet artifact slice with exact state/error distinction.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M3 — Hardening and contract audit
**Approach**: Harden ranking/rejection, retries, budgets, redaction, and cross-plan runtime-flag documentation.

**Public test seam**: Deterministic fake clock and transport verify deadlines, ambiguity rejection, and no leaked payloads.

**Wiring**: Add the Meet package to `get_all_tools()` explicitly and append Meet guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Candidate ties, timeout versus unavailable, authentication versus authorization, transport errors, and immutable flags.

**Tasks**: Add taxonomy tests, retry logic, provenance checks, skill/loader checks, and audit.

**Deliverable**: A tested Meet package with canonical resolution and safe artifact envelopes.

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