# 053 — Workflow plan: Google Drive tooling

## Objective

Expose exactly `drive.search`, `drive.list`, `drive.metadata`, and `drive.read`, implemented by Python `search`, `list`, `metadata`, and `read`. Read-only; no Drive mutations and no auth implementation.

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

- Every Drive tool MUST require `tools.google.read.enabled`; missing read defaults true.
  - AC: Disabled calls make zero credential/provider calls and omitted read allows reads.
- Drive MUST expose metadata and bounded approved content only; no mutation surface may be added.
  - AC: Adversarial payloads cannot produce raw provider fields, unsafe URLs, or unbounded content.
- Private Drive seams MUST be injectable and used by Meet/discovery without public-tool lookup.
  - AC: Tests verify metadata pre-read, content gating, exact identifiers, and private wiring.

## Scope and credentials

This plan covers Drive registration, read gating, metadata/search/list/read adapters, private correlation seams, tests, and the shared skill section. It does not cover mutations, OAuth storage, consent UI, API enablement, or arbitrary HTTP. A provider 403 is authorization and a provider 404 is not silently rewritten.

## Technical Design

### Policy and credential boundary

050 documents the top-level flags only and no longer owns a Google scope helper. Drive checks the read flag; resource IDs, service blocks, operation lists, and policy scope validation are not part of the runtime contract.

### Drive contract

Validate query at 1–500 characters, search limit 1–50, list limit 1–100, and file/drive IDs as non-empty control-free strings at most 256 characters. Every operation has a 30-second deadline. Public results are bounded metadata and normalized references.

Exact private seams are `async def search_references(query, limit, policy_snapshot)` and `async def read_reference(file_id, purpose, policy_snapshot)`. The first returns bounded metadata. The second validates the file ID and purpose, pre-reads metadata and correlation, and returns exactly `DriveReadResult` with bounded file, folder_id, format, content, truncated, provenance, and safe metadata fields.

Auto format resolves Docs/Slides to text, Sheets to CSV, allowlisted textual MIME to text, and other MIME to reference. The allowlist is `text/plain`, `text/markdown`, `text/csv`, `application/json`, and `application/xml`; approved Docs/Slides/Sheets exports only. Binary, unknown, executable, HTML, and unsafe formats remain references. Drive performs no mutations.

### Registration and loader

Decorate only the four exact functions, add an explicit Drive import to `get_all_tools()`, and append Drive guidance to `persona/skills/google/SKILL.md`. Discovery hands off `file.file_id` to `drive.metadata`; Meet uses private seams.

## Milestones
### M1 — Registration, gating, and metadata
**Approach**: Register four exact functions, implement bounds, read gating, fixed metadata, and injected client seams.

**Public test seam**: Fake Drive client and flag snapshot assert 30-second deadlines, zero denied calls, bounded IDs, and no arbitrary URLs.

**Wiring**: Add the Drive package to `get_all_tools()` explicitly and append Drive guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Query/limit boundaries, trashed files, shared-drive metadata, 403, malformed MIME, and deadline.

**Tasks**: Implement validators, metadata/list/search adapters, decorators, errors, and loader wiring.

**Deliverable**: Four registered Drive tools with exact bounds and metadata behavior.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M2 — Private reads and format matrix
**Approach**: Implement exact private seams, `DriveReadResult`, export/download allowlist, effective format, and normalization.

**Public test seam**: Inject metadata/export/download operations; assert metadata pre-read precedes content and denial prevents fetch.

**Wiring**: Add the Drive package to `get_all_tools()` explicitly and append Drive guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Docs/Slides/Sheets, text MIME, binary/unknown, malformed UTF-8, oversize fetch, line/output limits, and missing folder.

**Tasks**: Add read normalizer, truncation precedence, MIME matrix, provenance, and seam tests.

**Deliverable**: A complete exact DriveReadResult implementation usable privately by Meet.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M3 — Hardening and contract audit
**Approach**: Harden retries, 30-second deadlines, redaction, and shared runtime-flag documentation.

**Public test seam**: Transport fakes record timeout/retry and redaction; cross-plan tests compare exact paths and defaults.

**Wiring**: Add the Drive package to `get_all_tools()` explicitly and append Drive guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: 401 reload, 429/503, timeout, 403, stale metadata, secret text, and truncation regressions.

**Tasks**: Add boundary/security tests, skill/loader checks, and cross-plan audit.

**Deliverable**: A tested documented read-only Drive package and private Meet seams.

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