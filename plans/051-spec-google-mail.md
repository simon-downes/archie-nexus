# 051 — Workflow plan: Google Gmail tooling

## Objective

Provide Gmail reads and narrow mutations. The original single-message mutation surface is superseded by plan 056, which defines `mail.label`, `mail.archive`, `mail.unarchive`, `mail.mark_read`, `mail.mark_unread`, `mail.star`, and `mail.unstar` as bounded caller-batched operations. This plan remains authoritative for `mail.search`, `mail.read`, `mail.list_labels`, credentials, policy, normalization, and unsupported send/reply/forward/delete/attachment/arbitrary-HTTP surfaces.

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

- The implementation MUST gate reads on `tools.google.read.enabled` and Gmail mutations on `tools.google.write.enabled`; defaults are read true and write false. Plan 056 defines mutation-specific cache/read ordering.
  - AC: Read and write spies prove the two flags independently control calls.
- Gmail writes MUST use existing USER labels only, and system state changes MUST use the dedicated operations defined by plan 056.
  - AC: Tests reject label creation, send/delete, and unsupported system-label mutation.
- Inputs and outputs MUST be bounded, normalized, deterministic, and sanitized.
  - AC: Invalid or denied calls make zero provider calls and never expose credentials or raw payloads.

## Scope and credentials

This plan covers Gmail registration, read gating, the two narrow label/archive writes, adapters, tests, and the shared skill section. All other writes remain absent from the public surface. OAuth storage, consent UI, API enablement, and arbitrary HTTP are out of scope. A provider 403 is authoritative authorization; `OAuthCredential` cannot locally verify granted OAuth scopes.

## Technical Design

### Policy and credential boundary

050 documents the top-level flags and owns no Google scope helper. Gmail checks only the read flag for read tools and the write flag for the mutation operations defined by plan 056. No resource scopes, service blocks, or operation allowlists are parsed.

### Gmail contract

All inputs validate first. Query is 1–500 characters; limit is 1–100; message IDs and label names are control-free and at most 256 characters. Search/read return bounded metadata/body without attachments. `mail_list_labels` returns normalized existing labels. USER label names resolve internally to Gmail IDs; system-label mutations use plan 056's dedicated operations. Unknown or sensitive mutations reject.

Plan 056 defines the bulk mutation contract: callers provide 1–500 message IDs, the caller batches larger sets, and each operation makes one Gmail `batchModify` request without per-message pre-reads. Discovery hands off exactly `email.message_id`, never a thread ID.

### Registration, loader, and safety

Decorate the five exact functions once and add an explicit mail import to `get_all_tools()`. Append Gmail guidance to `persona/skills/google/SKILL.md`. No public send/delete operation is added.

## Milestones
### M1 — Registration, gating, and visibility
**Approach**: Register exact names, implement bounds, independent read/write gating, and label normalization.

**Public test seam**: Inject flags, credential/client, and a fake Gmail transport; assert zero calls when denied and exact labels.

**Wiring**: Add the mail package to `get_all_tools()` explicitly and append Gmail guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Wrong types, 500-character query, 1–100 limits, hidden messages, disabled flags, and 403.

**Tasks**: Implement decorators, validators, gating, normalization, loader wiring, and errors.

**Deliverable**: Five registered mail tools with exact visibility and gating.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M2 — Search/read and safe writes
**Approach**: Implement bounded search/read, exact USER-label mapping, and the read/write contracts delegated to plan 056 for mutations.

**Public test seam**: Fakes model current labels, missing labels, stale mutations, and already archived messages; assert no unsupported operation.

**Wiring**: Add the mail package to `get_all_tools()` explicitly and append Gmail guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Label creation attempts, duplicate changes, send/delete, uncertain responses, and no-op archive. Bulk mutation behavior is defined by plan 056.

**Tasks**: Add the read adapter, body normalization, existing-label normalization, and handoff tests. Mutation adapter and bulk-operation tests are defined by plan 056.

**Deliverable**: A complete bounded Gmail read/write slice without unsupported operations.

**Verify**: From repository root `/workspace/archie-nexus` run:

```bash
uv run pytest tests/test_google_*.py -q
uv run ruff check agent/src tests
```

**Acceptance criteria**: The milestone deliverable is present, its exact public/private seams and safety bounds are covered by deterministic tests, and the verification commands pass.

### M3 — Hardening and contract audit
**Approach**: Harden retries, body limits, redaction, deterministic labels, and shared flag documentation.

**Public test seam**: Transport fakes record retry/deadline behavior and secret payloads; compare exact runtime paths.

**Wiring**: Add the mail package to `get_all_tools()` explicitly and append Gmail guidance to `persona/skills/google/SKILL.md`.

**Edge Cases**: Oversize body, malformed headers, 429/503, timeout, 401 reload, and mutation uncertainty.

**Tasks**: Add boundary, redaction, retry, skill, loader, and cross-plan tests.

**Deliverable**: A tested documented Gmail package matching the locked public surface.

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