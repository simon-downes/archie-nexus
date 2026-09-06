# 037 — SPEC: Orchestrator auth API and OAuth flows

## Objective

Make the orchestrator the client-facing owner of Archie authentication flows while retaining the existing shared `~/.nexus/credentials.yaml` store. Expose typed provider configuration, credential status, static credential updates, OAuth login, and OAuth refresh through orchestrator HTTP APIs so local and remote clients use the same auth mechanism.

This spec separates Bedrock model-provider credentials from credentials used by Archie tools to access AWS. `bedrock` and `aws` remain distinct credential entries and provider identities even though both use AWS access-key fields.

## Context

Nexus already has a typed, YAML-backed credential subsystem under `shared/src/archie_shared/credentials/`, including provider descriptors, per-service msgspec credential structs, atomic store writes, PKCE/OAuth primitives, refresh logic, and the `archie auth bedrock` / CLI OAuth flows. The completed credential plan was host/CLI-oriented: OAuth callbacks are hosted by the CLI on `localhost:8585`, and the orchestrator's remote support currently exposes an unauthenticated whole-file `POST /credentials` endpoint.

The next auth slice must move the core flow to the orchestrator. A client requests effective provider configuration, starts OAuth through the orchestrator, opens the returned authorization URL, and observes completion. The orchestrator handles OAuth state, callback, code exchange, refresh, credential persistence, and status. Static credentials are submitted as typed JSON to a per-provider endpoint.

The current single-user trusted-network model remains in force. This spec does not introduce client/orchestrator authentication, a system keychain, a separate credentials mount, per-agent access controls, credential generations, or control-plane notifications to running agents. Existing shared-file visibility and agent propagation are separate work.

Agent-kit is the source of provider coverage and auth definitions. Its current auth definitions are:

- OAuth: `notion`, `slack`, `google`
- Static: `linear`, `github`, `aws`, `scalr`, `jira`
- Archie-specific static: `bedrock`

The agent-kit auth configuration must be represented in Nexus provider descriptors, including OAuth endpoints or discovery server, scopes, nested token paths, extra authorization parameters, and static field names.

## Requirements

### Provider configuration

- MUST expose `GET /auth/providers` returning the effective auth definition for every registered provider.
  - AC: Response includes `notion`, `linear`, `slack`, `github`, `aws`, `scalr`, `jira`, `google`, and `bedrock`.
  - AC: Each entry identifies its auth type (`oauth` or `static`) and required credential fields or OAuth metadata.
  - AC: Response contains no access tokens, refresh tokens, API keys, client secrets, or other secret values.

- MUST represent the agent-kit auth definitions in typed shared provider configuration models.
  - AC: Static definitions include `linear.token`, `github.token`, `aws.access_key_id/secret_access_key/session_token`, `scalr.token/hostname`, and `jira.email/token/cloud_id`.
  - AC: OAuth definitions include `notion` discovery via `server_url`, Slack nested token paths and `user_scope`, and Google scopes plus `access_type=offline` and `prompt=consent`.
  - AC: `bedrock` is a separate static provider from `aws`, with fields `aws_access_key_id`, `aws_secret_access_key`, and `aws_session_token`.

- MUST allow effective provider configuration to combine code-defined provider defaults with typed user configuration and persisted OAuth metadata.
  - AC: `NexusConfig.auth.providers` contains typed per-provider overrides; OAuth overrides include optional `client_id`, endpoints, scopes, extra parameters, and `expires_in_path`, while client secrets remain credential-store fields.
  - AC: Auth overrides are global to the orchestrator instance and are not selected by client profile; the running orchestrator loads them from its own `NexusConfig`, while `OrchestratorProfile` only supplies the client-side target and optional `public_url` for that instance.
  - AC: Precedence is code-defined provider defaults, then `config.yaml` provider overrides, then persisted OAuth endpoint/client-id metadata; a configured value overrides a default and persisted metadata does not override an explicit config value.
  - AC: Discovered endpoints and dynamically registered client IDs are persisted with the provider's credential/configuration state according to the existing Nexus convention; they are not written into the generic client configuration by the OAuth API.
  - AC: `OrchestratorProfile.public_url` is optional; when set it is the sole callback origin, otherwise local/direct request origin is used.

### Typed credential state

- MUST use `msgspec.Struct` for shared provider definitions, credential models, API request models, API response models, and OAuth flow/result models that cross a package or HTTP boundary.
  - AC: JSON requests and responses decode/encode through msgspec types.
  - AC: Credential structs use `forbid_unknown_fields=True`.
  - AC: Dataclasses are not introduced for wire or persisted models; private in-memory OAuth flow state MAY use a dataclass or msgspec struct, with msgspec preferred for consistency.

- MUST retain provider-specific credential structs without introducing a generic credential base class.
  - AC: `CREDENTIAL_TYPES` maps each provider name to its concrete credential type.
  - AC: `BedrockCredential` and `AwsCredential` are distinct types and store entries even though their fields are structurally similar.
  - AC: Provider-specific structural validation rejects unknown fields; flow-level validation handles required-field combinations and OAuth prerequisites.

- MUST provide typed credential status output that never returns secret values.
  - AC: Status identifies whether a provider has a stored credential, whether required fields are present, auth type, expiry where applicable, and an actionable state such as `missing`, `configured`, `valid`, `expired`, or `needs_reauthentication`.
  - AC: Status does not expose token/key values or secret-bearing object representations.
  - AC: Invalid stored data is reported as provider status/error information rather than causing secret contents to be logged.

### Credential store and core auth objects

- MUST retain `CredentialStore` as the persistence boundary over `home_dir() / "credentials.yaml"`.
  - AC: Existing atomic save, `0600` permissions, permission warning, typed lookup, and merge/update semantics remain available.
  - AC: Orchestrator auth handlers use the store API rather than manipulating YAML directly.
  - AC: Store updates preserve unrelated provider entries.

- MUST provide core auth objects that separate provider definition, credential state, and OAuth flow state.
  - AC: Provider definitions describe how authentication works.
  - AC: Credential state contains persisted provider-specific secrets and runtime token metadata.
  - AC: OAuth flow state contains short-lived state, PKCE verifier, provider, redirect URI, and expiry, and is not persisted to session history or credentials.

- MUST support per-provider credential replacement through `PUT /auth/credential/{provider}`.
  - AC: A valid static credential body is decoded against the provider's expected credential struct and replaces that provider entry atomically; omitted fields are removed rather than retained from the old entry.
  - AC: Unknown providers return HTTP 404 or 400 with an actionable error.
  - AC: Unknown fields, malformed JSON, or structurally invalid values return HTTP 400 without modifying the existing entry.
  - AC: Successful responses return redacted status only.
  - AC: OAuth token records are not accepted as a substitute for the OAuth login flow unless the provider's credential type explicitly supports manual token entry.

- MUST provide store-level replacement and deletion operations for provider entries.
  - AC: Replacement and deletion use one read-modify-write operation under the store's update lock and preserve unrelated providers.
  - AC: Concurrent updates to different providers do not lose either update.

- MUST support credential removal through `DELETE /auth/credential/{provider}`.
  - AC: Removing an existing provider deletes only that provider entry and returns a successful redacted status.
  - AC: Removing a missing provider is idempotent.

### OAuth

- MUST expose an orchestrator-owned OAuth login flow for registered OAuth providers.
  - AC: `POST /auth/login/{provider}` creates a short-lived flow and returns a flow ID plus authorization URL.
  - AC: The authorization URL contains PKCE S256 challenge, state, configured client ID, requested scopes, provider extra parameters, and the selected redirect URI.
  - AC: The orchestrator performs the authorization-code exchange after callback and persists access token, refresh token when returned, and ISO-UTC expiry.
  - AC: Successful login returns or makes available a redacted `valid` status.

- MUST expose `GET /auth/callback/{provider}` as the orchestrator callback for browser-reachable orchestrators.
  - AC: Callback validates provider, state, flow expiry, one-time-use state, and redirect URI before exchanging the code.
  - AC: Successful browser navigation receives a non-secret human-readable success page; failure receives a non-secret error page.
  - AC: Authorization codes, PKCE verifiers, and provider error details are not written to session logs or returned after the flow completes.
  - AC: A completed flow remains queryable through `GET /auth/flow/{flow_id}` for the flow's configured retention window with redacted outcome/status, then is removed; callback consumption and flow-store cleanup are distinct operations.

- MUST derive callback URLs from the incoming client/orchestrator origin with an explicit configured public URL override.
  - AC: A local request to `http://127.0.0.1:7600/auth/login/google` produces a callback on that same origin unless a configured override exists.
  - AC: A request to a remote private origin such as `https://archie.internal` produces `https://archie.internal/auth/callback/google` when that is the configured/served origin.
  - AC: The implementation does not blindly trust arbitrary forwarded-host headers; trusted proxy handling or an explicit `public_url` is used when the orchestrator is behind a proxy.
  - AC: Callback URL is included in the login response so the client can display/debug the selected flow.

- MUST support OAuth endpoint discovery and dynamic client registration where the provider definition requires it.
  - AC: Providers with `server_url` can discover authorization/token/registration endpoints before constructing the authorization URL.
  - AC: If registration is required and supported, the returned client ID (and client secret if provided) is persisted in the provider's stored state.
  - AC: Missing endpoints, client ID, or registration capability returns a clear actionable error without creating a usable partial flow.

- MUST expose non-interactive `POST /auth/refresh/{provider}` for OAuth providers.
  - AC: A valid refresh token produces updated access token and expiry, preserving a rotated refresh token when returned.
  - AC: Static providers and OAuth credentials without refresh state return an actionable reauthentication error.
  - AC: Refresh failures do not overwrite the previous valid credential record.
  - AC: Expiry uses the existing 60-second skew behavior and ISO-UTC timestamps, accepting provider-specific top-level or configured nested `expires_in` values.

### API behavior

- MUST remove the legacy `POST /credentials` route from the orchestrator.
  - AC: `POST /credentials` returns 404 after this change.
  - AC: No CLI or orchestrator code relies on whole-file credential upload for the new auth flow.

- MUST return consistent JSON errors for auth API failures.
  - AC: Client input/provider lookup/flow state errors return 4xx responses with a non-secret `error` message.
  - AC: External OAuth failures return a non-secret actionable error without raw token responses or secret-bearing exception text.
  - AC: Unexpected failures use the existing global 500 handling and do not disclose credential contents.

- MUST keep this spec independent of agent credential propagation.
  - AC: No new orchestrator-agent control channel, credential-update event, file watcher, environment synchronization, or generation index is required or implemented.
  - AC: Existing agents continue to discover credentials through the existing shared home mount and existing credential APIs.

## Technical Design

### Overview

Extend the existing shared credential subsystem with typed API-facing provider/configuration and OAuth flow models, then add an orchestrator auth service and routes. The orchestrator reads and writes the existing credential store and owns OAuth state and network calls. The CLI/TUI only needs to call the HTTP API and open the returned authorization URL; client UI integration beyond the HTTP contract is outside this spec.

### Technical Stack

- Reuse `msgspec>=0.19` for all shared and HTTP-bound models; this matches `shared/src/archie_shared/events.py`, `commands.py`, `schemas.py`, and the existing credential models.
- Reuse existing `httpx>=0.27` in `archie-orchestrator` for OAuth discovery, registration, code exchange, and refresh. Do not add another HTTP client.
- Reuse existing `starlette` route handlers and global error handling in `orchestrator/src/archie_orchestrator/app.py`.
- Reuse existing YAML-backed `CredentialStore` and atomic file operations. Do not add a keychain, database, separate credential mount, or new persistence dependency.
- Reuse the existing OAuth primitives in `shared/src/archie_shared/credentials/oauth.py`, but refactor them as needed so the orchestrator can provide a callback URI and own the exchange. Keep HTTP-dependent OAuth code out of modules imported by the container unless the existing lazy-import boundary is preserved.

### Architecture

**Shared credential/auth package**

- Owns provider definitions, typed credential structs, credential status types, store access, expiry calculations, token extraction, and OAuth protocol primitives.
- Does not own orchestrator HTTP request handling or in-memory flow lifecycle.

**Orchestrator auth service**

- Owns effective provider configuration, OAuth flow state, callback handling, token exchange, refresh, status computation, and calls to `CredentialStore`.
- Exposes the `/auth/*` HTTP API.
- Uses `home_dir()` to locate the same credentials file used by existing agents.

**Client**

- Calls provider/status/credential APIs and opens the returned authorization URL.
- Does not read remote orchestrator files or construct OAuth configuration independently.
- Client command/UI wiring is limited to preserving compatibility with the HTTP contract; no new TUI status surface is required in this spec.

### Data Model

**Provider descriptors**

Keep the internal `PROVIDERS` registry's existing `StaticProvider` and `OAuthProvider` structs for compatibility, and add separate client-facing tagged msgspec response structs rather than changing the registry's YAML/code shape in place. The response union uses an explicit `auth_type` discriminator and is encoded as JSON with `msgspec`.

- Common response fields: `name`, `auth_type`.
- Static response: `fields`, `can_refresh_noninteractive`.
- OAuth response: `server_url`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `scopes`, `token_path`, `refresh_token_path`, `extra_params`, `can_refresh_noninteractive`, optional effective `client_id`, and selected `redirect_uri`.

Client-facing provider responses MAY include effective `client_id` and redirect URI because they are non-secret; MUST omit `client_secret`. The service maps internal registry/config objects to response DTOs.

**Credential structs**

Retain and complete concrete structs in `shared/src/archie_shared/credentials/models.py`:

- `BedrockCredential`: `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`.
- `AwsCredential`: `access_key_id`, `secret_access_key`, `session_token`.
- `OAuthCredential` or provider-specific OAuth structs for `notion`, `slack`, and `google`, including access/refresh tokens, expiry, client ID, and persisted discovered endpoints/client secret where required by the existing convention.
- `LinearCredential`: `token`.
- `GithubCredential`: `token`.
- `ScalrCredential`: `token`, `hostname`.
- `JiraCredential`: `email`, `token`, `cloud_id`.

Keep all persisted fields optional where existing partial-store/status behavior requires it; enforce complete credential requirements at endpoint/flow validation time.

Add explicit `expires_in_path: str = "expires_in"` to OAuth provider configuration, with Slack configured as `authed_user.expires_in`; token normalization uses this path before falling back to the top-level field.

**Credential status**

Add a non-secret `CredentialStatus` msgspec struct containing at minimum:

- provider name;
- auth type;
- `configured`/presence state;
- validity state;
- optional `expires_at`;
- optional non-secret validation/error message.

**OAuth flow state**

Use a private runtime object keyed by a random `flow_id`, containing:

- provider;
- state;
- PKCE verifier;
- redirect URI;
- creation and expiry timestamps;
- consumed flag;
- outcome (`pending`, `succeeded`, or `failed`) and redacted status/error;
- any resolved client ID/token endpoint needed to complete the flow.

Keep flows in orchestrator memory for this single-process implementation. Callback consumption marks a flow consumed and stores its redacted outcome; each auth request calls `purge_expired_flows()`, and orchestrator lifespan shutdown clears the store. Completed flows are retained for a fixed 5-minute window after completion; pending flows expire after the configured 120-second login timeout. Do not persist flows.

### Data Flow

**Provider discovery/status**

1. Client requests `GET /auth/providers` or `GET /auth/status`.
2. Orchestrator loads effective config and/or `CredentialStore` state.
3. Orchestrator returns typed JSON with no secret values.

**Static credential update**

1. Client sends JSON to `PUT /auth/credential/{provider}`.
2. Handler resolves the provider and expected credential type.
3. Handler decodes and validates the body.
4. Handler writes the provider entry through `CredentialStore`.
5. Handler returns computed redacted status.
6. Unrelated provider entries remain unchanged.

**OAuth login**

1. Client calls `POST /auth/login/{provider}`.
2. Orchestrator resolves provider config, discovers endpoints/registers a client if needed, generates PKCE/state, and derives callback URI.
3. Orchestrator stores short-lived flow state in memory and returns authorization URL + flow ID + redirect URI.
4. Client opens the URL in the system browser.
5. Provider redirects the browser to `/auth/callback/{provider}`.
6. Orchestrator validates state/flow/provider/redirect URI and exchanges the code.
7. Orchestrator stores normalized token fields and expiry through `CredentialStore`.
8. Browser receives a success/failure page; the client can query status or flow state.

**OAuth refresh**

1. Client or future provider code calls `POST /auth/refresh/{provider}`.
2. Orchestrator loads current typed credential state.
3. It verifies refresh prerequisites and expiry policy.
4. It calls the provider token endpoint.
5. It writes the new token state atomically only after a successful response.
6. It returns redacted status.

### Error Handling & Edge Cases

- Unknown provider → HTTP 404 with provider name only.
- Static credential body has unknown fields or malformed JSON → HTTP 400; existing entry unchanged.
- Required static fields missing → HTTP 400 with field names, never values.
- Empty/null OAuth client configuration → HTTP 400 actionable error; no flow retained.
- OAuth provider discovery failure → HTTP 502 or 400 according to whether the provider is unavailable or configuration is invalid; no secrets in response.
- OAuth callback with unknown, expired, consumed, mismatched, or wrong-provider state → HTTP 400/browser error page; no token exchange.
- Provider returns OAuth error → browser error page and non-secret flow status; no credential write.
- Token exchange succeeds without an access token → reject response and preserve previous credential.
- Token response omits refresh token → retain the previous refresh token; replace it only when a new one is returned.
- Refresh fails → preserve previous credential and return reauthentication/provider-unavailable status as appropriate.
- Credential deletion for missing provider → idempotent success.
- Concurrent store writes → serialize replacement/deletion/read-merge-write with an advisory `fcntl.flock` lock on `credentials.yaml.lock`; lock acquisition is part of the store API and is released on every success/failure path.
- Client disconnects after starting OAuth → flow remains usable until expiry; status remains queryable by flow ID, then expires.
- Callback URL from untrusted forwarded headers → ignore untrusted headers and use configured public URL or the direct request origin.
- Callback query-string access code → configure the auth callback route/server access logger to omit query strings for `/auth/callback/*`; application logs still record only provider, flow ID, and outcome.

### External Integrations

**OAuth authorization servers**

- Purpose: endpoint discovery, optional dynamic registration, authorization-code exchange, and refresh.
- Pattern: awaited `httpx.AsyncClient` calls from orchestrator handlers, using the existing timeout and PKCE primitives; the client transport is injectable for tests.
- Constraints: provider-specific endpoints, nested token paths, scopes, and extra parameters come from typed provider definitions.
- Failure: do not write credentials on failed discovery, registration, exchange, or refresh; return redacted actionable errors.

**Browser**

- Purpose: user authorization interaction.
- Pattern: client opens the orchestrator-returned authorization URL using the system browser; provider redirects to the orchestrator callback when the browser can reach it.
- Constraint: callback URI must be registered/accepted by the provider. Remote private orchestrators require browser reachability; client-loopback callback fallback is outside this core API slice unless required by provider constraints discovered during implementation.

### Code Structure

- Extend `shared/src/archie_shared/credentials/providers.py` with client-facing typed provider configuration and any required effective-config helpers, following existing `StaticProvider`/`OAuthProvider` definitions.
- Extend `shared/src/archie_shared/credentials/models.py` with complete concrete credential mappings, preserving distinct `BedrockCredential` and `AwsCredential`.
- Extend `shared/src/archie_shared/credentials/store.py` only where the orchestrator API needs typed replacement/status support; keep YAML parsing and atomic persistence there.
- Add shared API models in `shared/src/archie_shared/credentials/api.py` (or an equivalently named auth-model module) for provider responses, credential status, credential requests, OAuth login responses, and flow status. Keep these msgspec types out of `orchestrator/app.py`.
- Add `orchestrator/src/archie_orchestrator/auth.py` for auth service state and provider/OAuth operations. Follow the existing small-module pattern used by `metrics.py` and `proxy.py`.
- Register routes in `orchestrator/src/archie_orchestrator/app.py`; route handlers should delegate to the auth service and return `JSONResponse`/`Response` consistent with existing handlers.
- Add `AuthError` subclasses or an equivalent explicit error taxonomy in `orchestrator/auth.py` for invalid input/provider configuration, OAuth-provider failure, expired flow, and credential validation failure; handlers map these deterministically to 400, 404, 502, or 409 as specified by tests. OAuth and credential exceptions passed to the global handler must use sanitized messages.
- Remove `push_credentials` and its `/credentials` route from `orchestrator/app.py` and update the architecture documentation route table/security section.
- Update `shared/src/archie_shared/credentials/oauth.py` primitives to accept caller-provided redirect URIs and support orchestrator use without assuming `localhost:8585`. Preserve CLI compatibility only where it does not conflict with orchestrator ownership.
- Add tests in existing root `tests/` following `test_orchestrator_credentials.py`, `test_nexus_config.py`, and OAuth tests from the completed credential work. Do not create a new test package unless the existing package layout requires it.

### Patterns and Conventions

- Use `msgspec.json.decode(..., type=...)` for request bodies and `msgspec.json.encode(...)` for typed responses.
- Keep `forbid_unknown_fields=True` on provider and credential structs.
- Return `JSONResponse({"error": ...}, status_code=...)` for expected handler errors; preserve the global exception handler for unexpected failures.
- Never log credential objects, raw request bodies, authorization codes, tokens, client secrets, PKCE verifiers, or provider token responses.
- Reuse `datetime.now(UTC)` and ISO-UTC serialization already used by credential refresh and auth CLI code.
- Keep OAuth HTTP dependency boundaries intact: shared code imported by the agent must not import `httpx` at module import time. The orchestrator already depends on `httpx`.

### Infrastructure and Deployment

- No new infrastructure, database, keychain, mount, or environment variable is required.
- Existing `ARCHIE_HOME_DIR` continues to determine the credential store location for the orchestrator and agents.
- Add `AuthConfig` to `NexusConfig` with a typed `providers` mapping; add `public_url: str | None = None` to `OrchestratorProfile`. Auth configuration is global to each running orchestrator, while profile `public_url` is the client-configured callback origin for that target.
- Existing trusted-network/no-client-auth behavior remains unchanged and should be documented as a limitation of these endpoints.

### Non-Functional Concerns

- Security: all auth API responses and logs MUST exclude secret values and OAuth codes/verifiers; callback state MUST be unpredictable, single-use, and expiry-bound.
- Reliability: failed external auth operations MUST leave the previous credential record intact; successful store writes MUST remain atomic.
- Observability: log provider and operation outcome (started/completed/failed) without request bodies, tokens, authorization URLs containing sensitive query values, or provider token responses.
- Compatibility: preserve existing typed store consumers and `archie auth bedrock` semantics while changing the orchestrator API; `bedrock` and `aws` MUST never be conflated by field names alone.

### Key Decisions

- Orchestrator-owned OAuth flow over CLI-owned callback: makes local and remote clients use the same API and lets the orchestrator persist tokens where its agents run; CLI localhost callback remains a fallback only if browser reachability requires it.
- Explicit configured public URL with direct-origin fallback: avoids incorrect remote callback URLs while allowing local `127.0.0.1:7600` operation without extra setup.
- `msgspec.Struct` for wire, persisted, and credential models: matches the existing Nexus contracts and provides strict JSON/YAML conversion; dataclasses are limited to private runtime state.
- No generic credential base class: provider-specific fields and the existing registry are clearer than inheritance, especially because Bedrock and AWS need distinct identities despite similar shapes.
- Separate `bedrock` and `aws` entries: model-provider credentials may intentionally belong to a different AWS account/org than tool credentials.
- Existing YAML store over a new secret system: matches the current trusted single-user deployment and keeps this slice focused on auth behavior.
- No control-plane notification in this spec: agent propagation is a later concern; the credential file and existing agent reads remain the source of compatibility.

## Milestones

### 1. Complete typed auth definitions and API models

**Approach:**
- Extend the existing shared credential registry rather than replacing it. Use msgspec structs and tagged provider definitions consistent with `models.py`, `providers.py`, and `schemas.py`.
- Preserve all agent-kit definitions and add/verify the Archie-specific `bedrock` descriptor. Keep `BedrockCredential` and `AwsCredential` concrete and separate.
- Add non-secret provider/status/login/flow request and response models in shared code so the orchestrator and future CLI/TUI clients share one contract.
- Test seam: direct msgspec encode/decode and provider-registry inspection.

**Edge cases:**
- Missing provider registration → registry validation test fails with the provider name.
- Unknown credential field → strict conversion rejects it.
- Bedrock/AWS same-shaped fields → registry still resolves different types and keys.

**Tasks:**
- Audit and update `models.py`, `providers.py`, and `CREDENTIAL_TYPES` for all nine providers.
- Add typed effective provider configuration/status/OAuth flow API models.
- Add typed `AuthConfig`/provider override fields to `NexusConfig`, including `auth.providers`, and add optional `public_url` to `OrchestratorProfile`.
- Add tests for agent-kit coverage, field definitions, OAuth metadata, distinct AWS identities, config precedence, and secret-free response encoding.
- Update shared exports without importing orchestrator-only HTTP code.

**Deliverable:** Shared typed auth definitions and API DTOs describe every supported provider and distinguish Bedrock from general AWS credentials.

**Verify:** `uv run pytest tests/test_credentials_models.py tests/test_credentials_providers.py -v` (new tests named in the tasks) and inspect encoded provider/status responses for absence of secret fields.

### 2. Add orchestrator provider and credential APIs

**Approach:**
- Add `orchestrator/auth.py` as the service layer; handlers in `app.py` only decode input, call the service, and map expected errors to HTTP responses.
- Reuse `CredentialStore` for all persistence and atomic writes. Static updates replace/merge only the addressed provider and return `CredentialStatus`.
- Implement `GET /auth/providers`, `GET /auth/status`, `PUT /auth/credential/{provider}`, and `DELETE /auth/credential/{provider}`.
- Test seam: ASGI HTTP requests through `httpx.AsyncClient` and a patched temporary `home_dir()`, matching `test_orchestrator_credentials.py`.

**Edge cases:**
- Unknown provider → 404/400 JSON error.
- Malformed/unknown-field body → 400 and unchanged file.
- Missing required fields → 400 with field names only.
- Existing unrelated services → preserved after update/delete.
- Missing deletion target → successful idempotent response.

**Tasks:**
- Implement auth service provider lookup, status computation, typed static credential replacement, and deletion.
- Add store-level `replace_credential()` and `delete_credential()` operations using a filesystem advisory lock around read-modify-write; replacement removes omitted fields and deletion is idempotent.
- Register the four routes and add response/error mapping.
- Remove `/credentials` and update its existing tests to assert it is absent.
- Add orchestrator API tests for all endpoint success/error paths and no-secret logging.

**Deliverable:** Clients can discover provider configuration, inspect redacted status, set typed static credentials, and remove credentials through the orchestrator API.

**Verify:** `uv run pytest tests/test_orchestrator_auth_api.py tests/test_orchestrator_credentials.py -v`; confirm `POST /credentials` returns 404 and unrelated credential entries survive updates.

### 3. Move OAuth login and refresh ownership to the orchestrator

**Approach:**
- Extend the existing OAuth primitives to accept orchestrator-derived redirect URIs while retaining PKCE S256, endpoint discovery, dynamic registration, dotted token extraction, and expiry calculation.
- Implement an in-memory `AuthFlowStore` in `orchestrator/auth.py` keyed by random flow ID. Store state/verifier/provider/redirect URI/expiry and enforce one-time consumption.
- Implement `POST /auth/login/{provider}`, `GET /auth/callback/{provider}`, `GET /auth/flow/{flow_id}`, and `POST /auth/refresh/{provider}`.
- Derive redirect URI from an explicit orchestrator `public_url` when configured, otherwise the direct request scheme/host. Do not trust arbitrary forwarded headers. Add `public_url: str | None = None` to `OrchestratorProfile` in `shared/src/archie_shared/schemas.py`, serialized as `public_url`, while preserving existing host/port defaults. When set, `public_url` is the sole callback origin.
- Use the existing orchestrator `httpx` dependency for network calls. Because Starlette handlers are async, use `httpx.AsyncClient` in the orchestrator auth service and await external calls. Define an async transport interface in `orchestrator/auth.py`; production uses `httpx.AsyncClient`, tests inject a fake transport. Do not make provider calls part of the test suite.
- Test seam: ASGI login/callback/flow/refresh HTTP contract plus mocked OAuth transport.

**Wiring:**
- State: `AuthFlowStore` is created during orchestrator lifespan and stored on `app.state.auth_service` or equivalent; it owns only short-lived in-memory OAuth flows.
- Producers: login handlers create flows; callback handlers consume them; expiry cleanup removes stale flows.
- Consumers: callback and flow-status handlers read flow state; `CredentialStore` receives normalized token updates after successful exchange/refresh.
- Call site: app lifespan initializes one auth service; route handlers call `auth_service.start_login(...)`, `complete_callback(...)`, `flow_status(...)`, and `refresh(...)`.

**Edge cases:**
- Discovery/registration unavailable → no usable flow and actionable non-secret error.
- State/provider/redirect mismatch → reject callback without token exchange.
- Expired/replayed flow → reject and remove flow.
- Token exchange without access token → preserve existing credential.
- Rotating refresh token → persist the new token; omitted refresh token → preserve old one.
- Refresh token missing/revoked → return reauthentication status and preserve old record.
- Browser callback success/failure → simple HTML response with no credential data.
- Remote private origin → generated callback is reachable only if the user browser can reach that origin; response exposes selected redirect URI.

**Tasks:**
- Add effective OAuth config resolution, public URL/origin handling, and flow lifecycle.
- Refactor OAuth helpers for caller-provided redirect URI and orchestrator exchange.
- Implement login, callback, flow status, and refresh routes.
- Normalize token responses into typed credential updates with ISO-UTC `expires_at`.
- Add mocked tests for PKCE URL parameters, discovery, registration, callback validation, exchange, refresh, expiry, redirect URL selection, and preservation on failure.
- Preserve or explicitly deprecate the old CLI OAuth implementation so it does not conflict with orchestrator-owned state.

**Deliverable:** A client can complete OAuth login and non-interactive refresh through the orchestrator, with tokens persisted atomically and no secret values returned or logged.

**Verify:** `uv run pytest tests/test_orchestrator_auth_oauth.py tests/test_credentials_oauth.py -v`; exercise a mocked login flow from `POST /auth/login/google` through callback and confirm the credential store contains normalized tokens while HTTP responses/logs do not.

### 4. Integrate the auth contract and documentation

**Approach:**
- Keep this milestone limited to contract consumers and documentation; do not implement control-plane notifications or agent environment propagation.
- Update CLI auth helpers only as needed to call the orchestrator APIs for remote profiles and to open the returned authorization URL with the existing `webbrowser` behavior. Preserve `archie auth bedrock` as a provider-specific import that targets `bedrock`, never `aws`.
- Update architecture documentation to replace `/credentials` with `/auth/*`, document orchestrator-owned OAuth, dynamic callback requirements, and the trusted-network limitation.

**Edge cases:**
- Remote profile unavailable → CLI reports the profile/orchestrator error without printing request secrets.
- Callback URL cannot be browser-reached → report the selected URL and failure; client-loopback fallback remains explicitly deferred.
- Local Bedrock import → stores only `bedrock` fields and does not alter `aws` credentials.

**Tasks:**
- Route `archie auth login`, `archie auth refresh`, `archie auth status`, and static `archie auth set` through the selected orchestrator's `/auth/*` APIs; the CLI opens the returned authorization URL and does not run its own callback server in the orchestrator-owned path.
- Remove whole-file `auth push` behavior tied to `POST /credentials`; replace it with provider-scoped API operations using `PUT /auth/credential/{provider}`. No remote whole-file push compatibility path remains.
- Update `docs/architecture.md`, README auth/config notes, and relevant tests.
- Add an end-to-end contract test covering client request → orchestrator response for provider discovery and status.

**Deliverable:** Nexus documentation and supported client auth operations use the orchestrator auth API without the removed whole-file credential endpoint.

**Verify:** `uv run ruff check && uv run pytest -v`; grep for remaining `/credentials` route/push dependencies and manually verify `archie auth bedrock` writes the `bedrock` entry separately from `aws`.

## Not yet specified

- Orchestrator-agent credential update notifications and control channel.
- Agent-side environment synchronization for AWS/GitHub CLI tools.
- Client/TUI auth status UI and slash commands beyond the HTTP contract.
- Client-loopback OAuth callback fallback for remote orchestrators whose browser cannot reach the orchestrator.
- Client↔orchestrator authentication, TLS, and untrusted-network hardening.

## Status

- Plan state: proposed
- Implementation: not started
- Control-plane notification/agent propagation: explicitly out of scope
