# 038 — Project: External SaaS tooling

## Objective

Add first-class access to external SaaS services from Archie agent sessions. Each service will be implemented as a separate spec, using the existing `exec/tools` convention and a service-specific skill, while sharing a small policy model that limits reads, writes, and provider-specific resources.

The first service is Jira. Later services should be able to adopt the same policy shape without requiring a new authorization design for every provider.

## Context

`archie-nexus/docs/saas-tooling.md` defines the intended execution boundary: decorated async functions grouped by service, available through `exec`, with service skills providing usage guidance. `archie-nexus/docs/auth-credentials.md` defines the existing typed credential store and the agent-container mount of the Nexus home directory.

`agent-kit` provides the capability inventory and prior permission experience for Jira, Linear, Slack, Notion, and Google integrations. It is reference material only; Nexus implementations must use the async agent tool architecture and existing shared credential/configuration boundaries rather than reproducing the CLI design.

The system is single-user, so this project should avoid a general multi-tenant authorization framework. The goal is a simple local safety boundary that prevents unnecessary writes and limits model-visible access to sensitive or potentially destructive SaaS operations.

## Locked Decisions

These decisions constrain every service spec.

- **D1: One spec per SaaS provider** — each provider receives one complete, directly implementable spec; capability-oriented milestones belong inside that spec.
- **D2: Shared policy location** — tool policies live in `config.yaml` under the generic top-level `tools` key, separate from `credentials.yaml`.
- **D3: Shared policy shape** — shared config uses a typed common mapping with opaque provider scopes:
  ```yaml
  tools:
    <provider>:
      read:
        enabled: true
        scope: ...
      write:
        enabled: false
        scope: ...
  ```
  The common model strictly validates the `read`/`write` blocks and stores `scope` as an opaque YAML value. Provider policy resolvers validate their own scope shape. Unknown provider entries are accepted for forward compatibility and ignored by tools that do not recognize them. `scope` may be omitted or empty.
- **D4: Safe defaults** — when a provider has credentials but no policy entry, reads are enabled and writes are disabled. A missing or empty scope means unrestricted access for that provider.
- **D5: Policy is local to the agent tool layer** — provider credentials and permissions remain the provider’s responsibility; Nexus adds a model-facing read/write/resource boundary before or while building provider requests.
- **D6: Credentials stay in the existing store** — service clients read typed credentials through `archie_shared.credentials` from the mounted `ARCHIE_HOME_DIR`. New services must not accept secrets as model/tool arguments. Environment projection is opt-in per provider and is not used for Jira.
- **D7: Exec-first exposure** — SaaS functions are initially available through `exec`, not the permanent native tool catalog. Extend the existing decorator/registry with explicit `exec_enabled`, `exec_docs`, and service namespace metadata so a function can be exec-enabled, omitted from permanent exec documentation, and exposed under a service namespace. Service skills document the functions. Provider specs may choose more restrictive exposure but must not expose secrets.
- **D8: Existing runtime configuration path and lifecycle** — the agent loads and owns the validated Nexus config at session startup. Each exec invocation receives the immutable non-secret tool-policy snapshot from the agent through the existing runner invocation context; the runner does not reread `config.yaml`. Policy changes require a new session/container and are not live control-plane updates. Jira credentials continue to be read from the mounted credential store. Any shared config schema changes are therefore visible when a new session starts.
- **D9: Bounded and safe results** — service functions validate inputs, bound result sizes/pagination, avoid returning unnecessary sensitive fields, and use distinct errors for configuration, policy, provider authorization, not-found, rate-limit, and transport failures where the provider supports those distinctions.
- **D10: Agent-kit is capability reference** — existing agent-kit commands and clients define the capability comparison and gap analysis, but not the Nexus module, client, sync/async, or permission implementation.
- **D11: Trust-boundary scope** — SaaS policy governs decorated SaaS functions only. It does not constrain arbitrary model-authored filesystem, subprocess, network, or Python-library access already available inside the trusted session container; this existing trust-boundary limitation is documented rather than solved by this project.

## Design

### Configuration

Add a typed `tools` section to the shared Nexus configuration: `dict[str, ToolPolicy]`, where `ToolPolicy` contains typed `read` and `write` policy blocks and each block contains `enabled` plus an opaque `scope: Any` defaulting to an empty value. Common-shape validation rejects malformed blocks, non-boolean `enabled`, and unknown fields inside a policy block. Unknown provider names are retained and ignored by unrecognized tools. Missing sections/blocks resolve to D4 defaults. Provider resolvers validate and normalize their own scope values.

Provider specs define the concrete scope type and enforcement behavior. They must document whether scope is enforced by generated provider queries, direct-resource checks, result filtering, or another provider-specific mechanism. Invalid scope for a recognized provider fails when that provider tool resolves policy, before credential/provider access; it does not prevent unrelated providers from starting.

### Runtime boundaries

- The orchestrator continues to own host configuration and credential persistence.
- The shared package owns policy schemas/resolution and credential models/store access.
- The agent owns service clients, decorated functions, provider error mapping, bounded formatting, and skills.
- The existing mounted Nexus home directory gives the agent access to credentials without adding a new credential transport.
- `exec` tool registration remains explicit through `exec/tools/__init__.py`; service packages must import their public modules to register functions.
- The first SaaS spec must add the shared decorator/registry capability required by D7 while preserving existing flat tool names and native-tool behavior. Service namespaces are registry metadata, not credential-provider registration; a credential provider may exist before tooling and a tool may be disabled when credentials are absent.
- Tool policy is loaded from `config.yaml` once by the agent at session startup, then passed as an immutable non-secret snapshot through the existing exec runner context. The runner never rereads the file; changing policy requires a new session/container.

### Exposure and guidance

Each service package should expose only its intended public functions from `__init__.py`. Implementation helpers and raw provider request methods remain undecorated. Skills must describe the available functions, required parameters, policy behavior, safe sequencing, and limitations without containing credentials.

### Permissions

The common policy distinguishes read and write enablement. Policy is loaded once by the agent per session and each exec invocation receives that immutable snapshot; it is not a live authorization service. Every operation is classified as read, write, or write-with-internal-read. Policy is checked before credential/provider access; a write-with-internal-read may perform only the provider reads explicitly documented by that provider spec. Policy errors must not disclose resources outside scope. Provider scopes are intentionally provider-specific. A service spec must identify:

- which functions are reads versus writes;
- how missing/empty scope behaves;
- whether scope can restrict direct resources, collections, mutations, or sensitive fields;
- what information may be revealed when policy blocks access;
- what the provider credential scopes can and cannot enforce.

## Slices

Each provider is one vertical project slice and one child spec. The child spec owns all provider capabilities and capability-oriented milestones; this project plan owns only the shared architecture and sequencing.

1. **M1 — Jira tooling**
   - Boundary: Complete Jira access mapped from agent-kit, including structured search, project-scoped policy, read/write gates, separate comments, mutations, and attachment safeguards.
   - Owns / exercises: D1–D11, especially the shared policy schema, exec namespace compatibility, and session policy snapshot.
   - Depends on: Nothing; this slice validates the shared seams.
2. **M2 — Linear tooling (placeholder)**
   - Boundary: To be sharpened after an agent-kit Linear capability and permission survey; one complete Linear spec when promoted.
   - Primary decisions exercised: D1–D7, D9–D11; D8 also applies to the slice. Any shared-seam change requires a revised project decision.
   - Depends on: M1 shared foundation.
3. **M3 — Slack tooling (placeholder)**
   - Boundary: To be sharpened after surveying channel/DM/read/write restrictions and OAuth behavior.
   - Primary decisions exercised: D1–D9, D11; D10 also applies to the slice. Any shared-policy expansion requires a revised project decision.
   - Depends on: M1 shared foundation.
4. **M4 — Notion tooling (placeholder)**
   - Boundary: To be sharpened after surveying page/database operations and resource scopes.
   - Primary decisions exercised: D1–D9, D11; D10 also applies to the slice.
   - Depends on: M1 shared foundation.
5. **M5 — Google Workspace tooling (placeholder)**
   - Boundary: To be sharpened after surveying Drive, Gmail, and Calendar capabilities and sensitive-data handling.
   - Primary decisions exercised: D1–D9, D11; D10 also applies to the slice.
   - Depends on: M1 shared foundation.

Only M1 is currently promoted to a child spec. M2–M5 remain fog-of-war placeholders until their surveys produce concrete objectives and boundaries.

## Sequencing

Build M1 first because it validates the common config and exec namespace seams. M2–M5 can be developed independently after M1’s shared foundation is accepted; their provider-specific scopes and capabilities must not be assumed to be interchangeable.

## Project acceptance criteria

The shared SaaS foundation is complete when:

- `NexusConfig` accepts strict common `tools` policy blocks with opaque scopes, applies safe defaults, accepts unknown provider entries, and rejects malformed common fields.
- Recognized-provider scope resolvers fail before credential/provider access when their opaque scope is invalid.
- Policy is a session-start snapshot and this behavior is covered by tests.
- Read, write, and write-with-internal-read operation classes have documented evaluation order and provider-specific internal-read rules.
- Existing flat exec/native tools retain their current behavior while namespaced exec-only tools can be registered, injected, and omitted from native/permanent documentation surfaces.
- Credential-provider registration and SaaS-tool registration remain independent and are covered by compatibility tests.

## Status

Project status: planning; Jira spec drafted and awaiting approval. Jira implementation is gated on approval of this project plan and completion of M1’s shared config/registry compatibility deliverables.

| Provider | Spec | Status |
|---|---|---|
| Jira | `039-spec-jira-tooling.md` | planned |
| Linear | — | not started |
| Slack | — | not started |
| Notion | `040-spec-notion-tooling.md` | planned |
| Google Workspace | — | not started |

## Open decisions deferred to provider specs

- Exact provider client/API implementation and dependency choices.
- Concrete scope representation and enforcement mechanism.
- Public function names, parameters, result formats, and milestone boundaries.
- Provider-specific sensitive fields and output redaction.
- Whether a provider needs additional credential fields or runtime projection.
- Provider-specific confirmation, idempotency, attachment, pagination, or rate-limit behavior.