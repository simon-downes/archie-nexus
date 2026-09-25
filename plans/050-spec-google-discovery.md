# 050 — Workflow plan: Google Workspace discovery search

## Objective

Add exactly `google.search`, implemented by Python `search(query, types=None, limit=20)`. It returns bounded metadata-only references across Gmail, Calendar, Drive, and conditionally Meet. It never returns bodies, document content, transcripts, notes, recording bytes, participant exports, tokens, or raw provider payloads.

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

- The implementation MUST gate all read work on `tools.google.read.enabled`, with missing read defaulting to true.
  - AC: Disabled reads make zero credential or provider calls; omitted read is allowed.
- The implementation MUST validate inputs before policy, credentials, clocks, or providers and return sanitized typed errors.
  - AC: Query, types, and limit boundary tests cover malformed, minimum, maximum, and just-outside values.
- The implementation MUST preserve bounded, deterministic, normalized results and private sibling seams.
  - AC: No public-tool lookup occurs inside discovery and no raw payload reaches an output.

## Scope and credentials

This plan covers discovery registration, read gating, source adapters, normalization, tests, and the shared skill section. It does not cover OAuth storage, consent UI, API enablement, arbitrary HTTP, or any new write surface. Credentials use the existing Google path; a 401 may use the existing reload boundary once, while a 403 remains authorization.

## Technical Design

### Policy and credential boundary

050 documents the two runtime flags only; it no longer owns or creates a Google scope helper. Discovery reads the resolved top-level read flag and does not interpret resource, service, or operation policy. OAuth configuration is separate manual provider setup.

### Registration and loader

Export only `search` with the exact `google.search` namespace key. Add an explicit discovery import to `get_all_tools()` and create the shared skill at `persona/skills/google/SKILL.md`; later plans append service guidance.

### Discovery contract

`query` is stripped to 1–500 characters. `types` is null or a non-empty unique list drawn from exactly `email`, `event`, `file`, and `meeting`. `limit` is a non-boolean integer from 1–20. Null types search email, event, and file and add Meet only when available; an explicit meeting request returns a sanitized configuration or authorization error when Meet is unavailable.

Each Gmail, Calendar, and Drive source may inspect two pages of 50 candidates; Meet correlation has a 50-candidate total. Global output is bounded by `limit`, deduplicated by canonical IDs, and contains bounded `type`, `id`, `title`, `summary`, `source`, `timestamp`, `url`, `provenance`, and `handoff`. IDs are at most 256 Unicode characters. Handoffs are exact: `email.message_id` to `mail.read`, `event.event_id` to `calendar.event`, `file.file_id` to `drive.metadata`, and meetings use canonical `calendar:<event_id>`, `meet:<conference_record_id>`, or `drive:<file_id>`.

## Milestones
### M1 — Contract, gating, and loader
**Approach**: Define validation, read gating, result/error envelopes, and injection before providers.

**Public test seam**: Inject client, credential, clock, and a read-enabled/disabled snapshot; assert exact envelopes and zero denied calls.

**Wiring**: Add the discovery package to `get_all_tools()` explicitly and create/maintain `persona/skills/google/SKILL.md`.

**Edge Cases**: Omitted read, disabled read, malformed types, duplicate IDs, unavailable Meet, and secrets in errors.

**Tasks**: Implement validators, exact decorator, loader import, bounded models, and sanitized errors.

**Deliverable**: One registered metadata-only `google.search` contract.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M2 — Bounded source slice
**Approach**: Implement Gmail, Calendar, Drive metadata search and conditional Meet correlation.

**Public test seam**: Fakes return duplicates, malformed records, and provider errors; assert caps and excluded sources are not probed.

**Wiring**: Add the discovery package to `get_all_tools()` explicitly and create/maintain `persona/skills/google/SKILL.md`.

**Edge Cases**: 401 reload, 403, 404, partial failure, duplicate IDs, and no eligible sources.

**Tasks**: Add adapters, correlation, normalization, ordering, and failure classification.

**Deliverable**: One end-to-end bounded discovery slice.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M3 — Hardening and documentation
**Approach**: Enforce field limits, deadlines, retries, redaction, and cross-plan documentation.

**Public test seam**: A deterministic clock and transport record retries, deadlines, and partial-failure truncation.

**Wiring**: Add the discovery package to `get_all_tools()` explicitly and create/maintain `persona/skills/google/SKILL.md`.

**Edge Cases**: Oversize Unicode, timeout, retry exhaustion, unsafe URLs, and unstable ordering.

**Tasks**: Add boundary tests and audit flags, skill content, handoffs, and loader wiring.

**Deliverable**: A tested, documented discovery tool ready for integration.

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