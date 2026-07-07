# 004 — Credential Subsystem Overhaul

## Objective

Replace the Bedrock-only `credentials.py` in `archie-shared` with a general,
typed, multi-service credential subsystem: typed msgspec structs per service, a
code-defined provider registry, an atomic credential store, and an OAuth2 + PKCE
login/refresh engine (host-side). Covers all eight agent-kit services and lays
the groundwork for future in-container credential self-refresh (deferred).

## Context

The current nexus credentials implementation
(`shared/src/archie_shared/credentials.py`) is a flat, untyped dict store hard-
coded around a single `bedrock` service (`SERVICE_BEDROCK`). It cannot represent
OAuth tokens, per-service field schemas, or discovered OAuth endpoints, and its
`save_credentials` is non-atomic (write-then-chmod) with a merge-vs-replace
staleness bug.

The **agent-kit** project (`/Users/simon.downes/dev/archie/agent-kit`) already
implements a working multi-service credential + OAuth model we port *concepts and
code* from (not a blind lift):
- `src/agent_kit/auth/oauth.py` — PKCE (S256), RFC 8414 two-hop endpoint
  discovery, RFC 7591 dynamic client registration, code exchange, token refresh,
  auth-URL builder, and a localhost callback server (`localhost:8585`, 120s).
- `src/agent_kit/auth/cli.py` — `login` / `refresh` / `status` commands, a
  dotted-path token extractor (`_extract`), and token storage (`_store_tokens`,
  ISO-UTC `expires_at`).
- `src/agent_kit/config.py:10-50` — `DEFAULT_CONFIG["auth"]` defines the eight
  provider descriptors (static vs oauth) we reproduce as code structs.

**Divergences from agent-kit (deliberate):**
- agent-kit's `login_cmd` mutates `config.yaml` (writes discovered endpoints /
  `client_id` back into config). **Nexus does NOT** — discovered OAuth endpoints
  and the registered `client_id` are stored in the *credential entry* instead, so
  `config.yaml` stays declarative.
- Credentials are **typed msgspec structs** per service (agent-kit uses untyped
  dicts).

**Dependency (must land first):** plan 003 (amended) delivers the config-dir
migration this plan builds on — `~/.nexus` HOME directory, single `ARCHIE_HOME_DIR`
env var (host default `~/.nexus`, container `/home/${USERNAME}/.nexus`), and
`config.py:home_dir() -> Path`. This plan resolves the credential store as
`home_dir() / "credentials.yaml"`.

**Scope boundary (this iteration):** the in-container agent consumes **only
bedrock** credentials. Agent-side credential self-refresh + write-back is
**deferred** to a later plan. The store is mounted **read-only** into the
container. OAuth login is **host-only**.

## Requirements

### Typed credential store

- MUST define one msgspec Struct per service holding that service's fields
- MUST use `forbid_unknown_fields=True` and make **all fields Optional (`| None`,
  default `None`)** so partially-populated entries load without error. This means
  **every field that may ever be persisted for a service must be pre-declared on
  its struct** — including OAuth fields discovered/registered at login time
  (`authorization_endpoint`, `token_endpoint`, `registration_endpoint`,
  `client_id`, `client_secret`). There is no free-form overflow; a field not on
  the struct is a validation error on the next load.
- MUST cover the eight agent-kit services — `notion`, `linear`, `slack`,
  `github`, `aws`, `scalr`, `jira`, `google` — plus `bedrock`, for **nine
  credential types total** (eight from agent-kit + bedrock)
- MUST persist as YAML keyed by service name: `{service: {field: value}}`
- MUST load as `dict[str, dict]`, then `msgspec.convert` each entry to its typed
  struct on demand (service name is the dict *key*, not a struct tag → unknown
  *service* keys are NOT caught by msgspec; only unknown *fields* within a known
  service are). Unknown service keys are tolerated (ignored/preserved).
- MUST validate prerequisite fields at **flow time** (login / refresh), not at
  load time
- MUST write atomically: temp file in the same directory, `chmod 0600`,
  `os.replace` (rename). Whole-store read-merge-write, last-writer-wins, **no
  lock**
- MUST warn (not fail) if an existing store file has permissions broader than
  0600

### Provider registry (code-defined)

- MUST define providers in code as msgspec structs: `StaticProvider` (name +
  `fields: list[str]`) and `OAuthProvider` (endpoints, scopes, `token_path`,
  `refresh_token_path`, `extra_params`, optional `server_url`)
- MUST expose a `PROVIDERS: dict[str, StaticProvider | OAuthProvider]` registry
  covering all eight agent-kit services (see below)
- MUST NOT write discovered OAuth endpoints / `client_id` back to `config.yaml`
  (they go into the credential entry)

### OAuth engine (host-side)

- MUST port PKCE S256, endpoint discovery, dynamic registration, code exchange,
  token refresh, auth-URL builder, and the localhost callback server from
  agent-kit `oauth.py` (sync `httpx`)
- MUST provide non-interactive refresh usable from `archie auth refresh <svc>`
- MUST store OAuth results with an ISO-UTC `expires_at` derived from `expires_in`

### Refresh engine

- MUST provide `is_expired(expires_at, skew=60)` (60s skew), a per-credential
  `can_refresh_noninteractive` signal, and `refresh_credential(...)`
- MUST raise `InteractiveReauthRequired` when a credential cannot be refreshed
  non-interactively (e.g. bedrock/SSO) so callers surface an actionable error
- Bedrock's `can_refresh_noninteractive` is **False** (interactive host re-auth
  only)

### CLI (host)

- MUST keep `archie auth bedrock` (boto3 credential-chain import) working against
  the new store
- MUST add `archie auth login <service>` (OAuth PKCE, host-only, localhost:8585),
  `archie auth refresh <service>` (non-interactive OAuth refresh), and update
  `archie auth status` to list all configured services
- SHOULD add `archie auth set <service> <field=value>...` for static providers
  (import/manual entry)

### Container integration

- MUST mount the credential store as part of the home **directory** (`home_dir()`)
  read-only at `/home/${USERNAME}/.nexus`, alongside config (shared dir mount from
  plan 003); container reads `credentials.yaml` via `ARCHIE_HOME_DIR`
- MUST keep the directory-mount shape so a future plan can flip `:ro` → `:rw`
  without re-plumbing
- The in-container `BedrockClient` MUST read bedrock credentials via the new
  store API; its existing reactive reload-on-error behaviour is preserved

### Dependencies

- MUST add `boto3>=1.35` to `cli/pyproject.toml` (currently lazily imported in
  `cli/auth.py` but undeclared)
- `httpx` is CLI-side only (cli already has `httpx>=0.27`); MUST NOT add httpx or
  boto3 to `shared` or `agent`

## Design

### Module layout

New package `shared/src/archie_shared/credentials/` (replaces the flat
`credentials.py`):

- `models.py` — one Credential struct per service; all fields
  `Optional`/`None`; `forbid_unknown_fields=True`.
  - `BedrockCredential`: `aws_access_key_id`, `aws_secret_access_key`,
    `aws_session_token`
  - `OAuthCredential` (shared shape for oauth services): `access_token`,
    `refresh_token`, `expires_at`, `client_id`, `client_secret`,
    `authorization_endpoint`, `token_endpoint`, `registration_endpoint`.
    **All discoverable/registrable OAuth fields are pre-declared here** so that
    endpoints and `client_id` obtained during `archie auth login` can be stored
    in the credential entry (not `config.yaml`) without tripping
    `forbid_unknown_fields`.
  - Static-service structs: `LinearCredential(token)`,
    `GithubCredential(token)`, `ScalrCredential(token, hostname)`,
    `JiraCredential(email, token, cloud_id)`, `AwsCredential(access_key_id,
    secret_access_key, session_token)`
  - A `CREDENTIAL_TYPES: dict[str, type]` mapping service → struct for
    `msgspec.convert`
- `providers.py` — `StaticProvider`, `OAuthProvider` structs +
  `PROVIDERS` registry. Reproduces `DEFAULT_CONFIG["auth"]` from agent-kit
  `config.py:15-50`:
  - static: `linear` (token), `github` (token), `aws` (access_key_id,
    secret_access_key, session_token), `scalr` (token, hostname), `jira`
    (email, token, cloud_id)
  - oauth: `notion` (discovery via server_url), `slack`
    (token_path=`authed_user.access_token`,
    refresh_token_path=`authed_user.refresh_token`, user_scope extra_params),
    `google` (scopes + extra_params `access_type=offline`, `prompt=consent`)
  - `bedrock`: treated as a static-like provider whose credentials are
    populated by `archie auth bedrock`; `can_refresh_noninteractive=False`
- `store.py` — store resolution + atomic IO:
  - `store_path() -> Path` = `home_dir() / "credentials.yaml"`
  - `load_store() -> dict[str, dict]` (perm warning; empty → `{}`)
  - `save_store(data: dict) -> None` (temp+rename+chmod0600 in same dir)
  - `get_credential(service) -> struct | None` (`msgspec.convert` of the entry)
  - `set_credential(service, fields: dict)` (read-merge-write whole store)
- `oauth.py` — ported from agent-kit `oauth.py`: `generate_pkce`,
  `discover_endpoints`, `register_client`, `exchange_code`, `refresh_token`,
  `build_auth_url`, `open_browser`, `_CallbackHandler`, `wait_for_callback`.
  (Lives under `shared` but only imported host-side; `httpx` stays a CLI dep.
  **Not re-exported from `credentials/__init__.py`** — see httpx guard below.)
- `refresh.py` — `is_expired(expires_at, skew=60)`,
  `can_refresh_noninteractive(service) -> bool`, `refresh_credential(service)`,
  `InteractiveReauthRequired(Exception)`. Imported by the agent, so it **must
  import `oauth` lazily inside the oauth-refresh code path**, never at module
  top-level (httpx guard).

Delete the old flat `credentials.py`, `SERVICE_BEDROCK`,
`CREDENTIALS_PATH`/`CONTAINER_CREDENTIALS_PATH`. Migrate all importers
(`agent/llm/bedrock.py`, `cli/auth.py`, `cli/cli.py`, `shared/__init__.py`).

### Store semantics (key detail)

The store is `dict[str, dict]` on disk. On read we do **not** decode the whole
file into a single struct — because the service name is a dict *key*, msgspec
cannot tag-dispatch it and cannot reject unknown *services*. Instead, per lookup
we `msgspec.convert(entry, CREDENTIAL_TYPES[service], strict=False)`, which *does*
reject unknown *fields* within a known service (`forbid_unknown_fields=True`).
Unknown top-level services are ignored on read and preserved on write.

### Tests

Create new `shared/tests/` (does not exist yet) with a package marker.
- `test_credential_store.py`: load/save round-trip, atomic rename, 0600, perm
  warning, empty file, partial entry loads, unknown-field rejection, unknown-
  service tolerance
- `test_providers.py`: registry completeness (8 services), static/oauth typing
- `test_refresh.py`: `is_expired` skew boundary, `can_refresh_noninteractive`
  per service, `InteractiveReauthRequired` for bedrock
- OAuth flow: unit-test `generate_pkce`, `build_auth_url`, `_extract`,
  `_store_tokens` expires_at math (mock httpx for network calls)

---

## Milestones

### 1. Config-dir + credentials path

**Approach:**
- Depends on plan 003's `home_dir()`. Introduce
  `store.store_path() = home_dir() / "credentials.yaml"`.
- Create the `credentials/` package skeleton (`__init__.py`) alongside the old
  flat module (not yet deleted).

**Tasks:**
- Create `shared/src/archie_shared/credentials/__init__.py`
- Add `store_path()` in `store.py`
- Create `shared/tests/__init__.py`

**Deliverable:** store path resolves relative to `ARCHIE_HOME_DIR`.

**Verify:** `uv run python -c "from archie_shared.credentials.store import store_path; print(store_path())"`.

### 2. Typed store (models + store IO)

**Approach:**
- Implement `models.py` structs + `CREDENTIAL_TYPES`.
- Implement `store.py`: `load_store`, `save_store` (temp+rename+chmod0600 same
  dir via `os.replace`), `get_credential` (`msgspec.convert`), `set_credential`
  (read-merge-write).

**Edge cases:**
- Missing file → `{}`. Empty YAML (None) → `{}`.
- Permissions broader than 0600 → warn to stderr, still load.
- Partial entry (some fields None) → loads. Unknown field → ValidationError
  wrapped with service + path. Unknown service key → ignored on read, preserved
  on write.

**Tasks:**
- Implement `models.py`, `store.py`
- Write `shared/tests/test_credential_store.py`

**Deliverable:** typed get/set with atomic writes.

**Verify:** `uv run pytest shared/tests/test_credential_store.py -v`.

### 3. Provider registry

**Approach:**
- Implement `providers.py` with `StaticProvider`/`OAuthProvider` structs and the
  `PROVIDERS` dict (all 8 services), reproducing agent-kit `config.py:15-50`.

**Tasks:**
- Implement `providers.py`
- Write `shared/tests/test_providers.py`

**Deliverable:** `PROVIDERS` registry with correct static/oauth descriptors.

**Verify:** `uv run pytest shared/tests/test_providers.py -v`.

### 4. Refresh engine

**Approach:**
- Implement `refresh.py`: `is_expired(expires_at, skew=60)`,
  `can_refresh_noninteractive(service)` (False for bedrock; True for oauth
  services with a refresh_token), `refresh_credential(service)`,
  `InteractiveReauthRequired`.
- `refresh_credential` for oauth uses `oauth.refresh_token` (milestone 5) — stub
  the call site here and wire in milestone 5, or land 5 first if simpler.

**Tasks:**
- Implement `refresh.py`
- Write `shared/tests/test_refresh.py`

**Deliverable:** expiry + refreshability logic; interactive-reauth signalling.

**Verify:** `uv run pytest shared/tests/test_refresh.py -v`.

### 5. OAuth primitives + host login/refresh CLI

**Approach:**
- Port agent-kit `oauth.py` verbatim-ish into `credentials/oauth.py` (drop
  `client_name` → "Archie"; keep localhost:8585 / 120s).
- In `cli/auth.py` add `login`, `refresh` commands and a `_store_tokens` helper
  (ISO-UTC `expires_at` from `expires_in`; dotted `_extract` for token paths).
  **Do NOT write discovered endpoints/client_id to config.yaml** — persist them
  into the credential entry via `set_credential`.
- **httpx guard:** `httpx` is a CLI-only dependency; the agent container has no
  httpx. Enforce this structurally, not by discipline alone:
  - Do **NOT** re-export `oauth` (or its symbols) from
    `credentials/__init__.py`. Callers must `from
    archie_shared.credentials.oauth import ...` explicitly (host/CLI only).
  - Inside `oauth.py`, `import httpx` at module top-level is fine *because the
    module is never imported by container code*; the `__init__.py` non-export is
    what keeps `from archie_shared.credentials import ...` httpx-free.
  - `refresh.py` (imported by the agent) must import `oauth` lazily *inside* the
    functions that need it (oauth-refresh path), never at module top-level.

**Edge cases:**
- state mismatch → CSRF error; timeout → error; error param → error.
- provider needs discovery but no server_url → actionable error.

**Tasks:**
- Implement `credentials/oauth.py`
- Add `archie auth login`, `archie auth refresh` in `cli/auth.py`
- Update `archie auth status` to iterate all configured services
- Add `boto3>=1.35` to `cli/pyproject.toml`; run `uv sync`
- Tests: pkce/build_auth_url/_extract/_store_tokens (mock httpx)

**Deliverable:** host OAuth login + non-interactive refresh working.

**Verify:** `uv run ruff check && uv run pytest -v`.

### 6. Bedrock bootstrap + BedrockClient migration + delete old module

**Approach:**
- Rewrite `archie auth bedrock` to write via `set_credential("bedrock", ...)`
  using `BedrockCredential` fields.
- Update `agent/llm/bedrock.py` `_create_client` / `_try_refresh_credentials` to
  use `get_credential("bedrock")` (typed struct) instead of
  `get_service_credentials(SERVICE_BEDROCK)`. Preserve the reactive reload-on-
  error behaviour.
- On refresh failure surface `InteractiveReauthRequired`-derived guidance
  (bedrock `can_refresh_noninteractive=False`).
- Delete the old flat `credentials.py` + `SERVICE_BEDROCK`; migrate
  `shared/__init__.py` re-exports; update any remaining importers.

**⚠️ Gotchas:**
- `bedrock.py` field names: `aws_access_key_id`, `aws_secret_access_key`,
  `aws_session_token` → now struct attrs. Guard on `None`.
- The container has boto3 (agent dep) but NOT httpx. Enforce the httpx guard:
  `credentials/oauth.py` is **not re-exported** from `credentials/__init__.py`,
  and `refresh.py` imports `oauth` lazily inside functions. Verify no
  container-loaded module (`agent/**`, `shared` top-level `__init__`) imports
  `oauth` at module scope.

**Tasks:**
- Rewrite `cli/auth.py` bedrock + status commands
- Migrate `agent/llm/bedrock.py` (`_create_client`, `_try_refresh_credentials`)
- Delete old `credentials.py`; update `shared/__init__.py`
- Fix affected tests (`test_bedrock.py` etc.)

**Deliverable:** bedrock end-to-end on the new store; old module gone.

**Verify:** `uv run ruff check && uv run pytest -v`.

### 7. Container mount rewrite

**Approach:**
- In `cli/cli.py` (docker run, ~246-272): replace the two per-file mounts
  (`ARCHIE_CONFIG=/archie/config/nexus.yaml` + the conditional
  `nexus.creds.yaml` file mount + `ARCHIE_CREDENTIALS`) with a **single
  directory mount** of `home_dir()` at `/home/${USERNAME}/.nexus:ro` plus
  `ARCHIE_HOME_DIR=/home/${USERNAME}/.nexus` (config path piece comes from plan 003).
- Drop the `CREDENTIALS_PATH`-exists conditional file-mount block; the directory
  mount carries `credentials.yaml` if present.
- Keep the mount as a directory (`:ro` now) so a future plan flips to `:rw`.

**⚠️ Gotchas:**
- Directory mount + atomic-rename on the host = container sees new inode on
  reload (this is the intended pattern; single-file bind mount would pin the
  inode and serve stale reads).
- If the home dir doesn't exist on the host, create it (or warn) before mount.

**Tasks:**
- Rewrite docker-run mount/env block in `cli/cli.py`
- Remove `from archie_shared.credentials import CREDENTIALS_PATH` usage
- Manual verify: `archie start` with a bedrock credential present; agent reaches
  Bedrock

**Deliverable:** container reads config + credentials from one `:ro` dir mount
via `ARCHIE_HOME_DIR`.

**Verify:** `archie build && archie start`, attach, send a message, confirm a
Bedrock response; `uv run ruff check && uv run pytest -v`.

---

## Deferred (explicitly out of scope)

- Agent-side (in-container) credential self-refresh + write-back to the store
  (dissolves the write-back concurrency hole). Store stays `:ro` this iteration.
- Host/container uid-ownership handling on store writes (N1) — lands with
  write-back.
- `~/.archie` → `~/.nexus` existing-file migration hint (N4).
