# 090 — Agent config and credentials from the orchestrator

Status: planning
Branch: build-it/090-agent-config-fetch (base: main @ 8cdad07)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/090-agent-config-fetch

## Objective

Agent containers stop reading `config.yaml` and `credentials.yaml` from the mounted
`~/.nexus`. The agent fetches config and credentials from the orchestrator over an
authenticated HTTP route, keeps them in an in-memory pull-through cache, and serves
subprocesses (the exec runner, the `gh` wrapper, the AWS `credential-process`) through a
Unix socket. A missed call site fails loudly instead of silently reading the still-mounted
files.

## Context

The user's goal is to prevent incidental exposure of config and credentials to the model
(a model `cat`-ing or editing the files while debugging, or following an injected
instruction), not to defend against a determined model. It is a single-user local system;
ease of implementation is the priority. Design agreed in discussion (2026-11-15):

- The orchestrator issues a per-session token, `ARCHIE_AGENT_TOKEN`, and stores it in the
  `sessions` table. The agent sends it as a bearer token on `/api/agent/*`.
- The agent keeps config and credentials in memory: a credential miss fetches from the
  orchestrator (absence is cached too); a sync every 5 minutes asks "anything newer than
  these versions?"; versions are content hashes. A cached OAuth credential whose
  `expires_at` is under 3 minutes away is synced when used.
- Processes other than the agent reach the cache through a Unix socket served by the
  agent with GET-only routes, never through the agent's `0.0.0.0:8080` server (published
  to host loopback).
- The token is removed from the agent's `os.environ` at startup so shells don't see it.
- A council (app developer, QA, SRE, pragmatist; 4/4) chose: `shared` stays file-based for
  host callers; agent call sites switch explicitly to an agent-owned cache module; a
  fail-loud trip-wire in `shared` (`ARCHIE_AGENT_CONTAINER=1`) raises when a shared file
  reader runs in a container; the root `conftest.py` clears the flag and targeted tests set
  it; the agent refuses to start without the flag.
- The `~/.nexus` mount stays as it is; removing the two files from the container is later
  work. `archie.db`, the brain, and the `agent/`/`persona/` mounts are out of scope.

Codebase facts:

- Containers reach the host orchestrator at `http://host.docker.internal:7600` (verified
  from this container on colima, about 1 ms per request). `prepare_start`
  (`orchestrator/.../lifecycle.py`) builds the `docker run` command; `run_container`
  (`docker.py`) runs it and never logs argv.
- Agent readers of the files: `app.py` lifespan (`load_nexus_config`),
  `tool_policy_live.py` (policy before each exec), `skills.py` (enabled plugins),
  `exec/tools/_runtime_env.py` (calls `shared` `runtime_environment()`, which calls
  `get_credential`), `llm/bedrock.py`, `llm/bedrock_openai.py`, `llm/openai_api.py`,
  `kiro/harness.py` (`resolve_api_key`), `exec/plugin.py` `CredentialSource.get()` (runs
  in the exec runner subprocess). Bash: `container/bin/gh`,
  `plugins/aws/bin/credential-process`. The Kiro MCP server runs inside the agent process.
- `/run` is root-owned and not writable by the container user; `/tmp` is.
- OAuth refresh on the host: every 60 s, for tokens expiring within 5 minutes
  (`credential_refresh.py`).

## Baseline

- Check commands: `env -u ARCHIE_BACKEND scripts/dev test -q -p no:cacheprovider`;
  `scripts/dev ruff check .`; `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: `.venv-archie` (created by sync; gitignored)
- Failing before this work: none with `ARCHIE_BACKEND` unset (5531 passed). This session
  runs on Kiro, so its environment has `ARCHIE_BACKEND=kiro`, which makes 14 tests fail and
  9 error in `tests/test_question_tool.py` and `tests/test_ws_integration.py`
  (pre-existing, unrelated); checks run with it unset.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout)

## Decisions

Rows marked "user" were settled in the discussion before the build (no interview
questions were needed).

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Token storage | SHA-256 hex of the token in a new `sessions.agent_token_hash` column (migration 4) | plaintext token | `archie.db` sits in the container's `~/.nexus` mount; the check only needs the hash, and restart recovery still works | user (table), self (hash) |
| D2 | Token delivery | `-e ARCHIE_AGENT_TOKEN` (name only) in argv, value in the `docker` CLI's environment | `-e ARCHIE_AGENT_TOKEN=<value>` | `DockerError` text (argv and stderr) is stored as the session's `error` and shown in clients | self |
| D3 | Route shape | one `POST /api/agent/sync`: startup fetch, a miss (version `null`) and the periodic sync are the same call | separate GET config / GET credential / sync routes | one code path; a miss is "I hold no version" | self |
| D4 | Config served | the full validated `NexusConfig` from `config_source.get()` | a filtered subset | it stays in agent memory and never reaches subprocesses; keeps the `NexusConfig` type intact | self |
| D5 | Wire types | `msgspec` structs in new `shared/src/archie_shared/agent_sync.py` | duplicated in agent and orchestrator | agent↔orchestrator contract; CONTRIBUTING puts contracts in `shared` | self |
| D6 | Where the switch lives | new agent module `archie_agent.host_config`; agent call sites switch explicitly; `shared` readers stay file-based | container mode inside `shared` readers | council 4/4: no hidden network I/O, no silent fallback | user (council) |
| D7 | Trip-wire | `guard_host_files()` in `shared/config.py` raises `HostFileAccessError` when `ARCHIE_AGENT_CONTAINER=1`; called by `load_nexus_config()` (default path) and `store_path()` | `/.dockerenv` detection | archie-nexus is developed inside Archie containers, so `/.dockerenv` would trip host tests | user (council) |
| D8 | Explicit-path config loads | `load_nexus_config(path)` is not guarded | guard every call | CLI and `archie config` validate temp files by explicit path; the guard targets the two host files | self |
| D9 | Test default for the flag | root `tests/conftest.py` clears it; guard and host-config tests set it | on by default for agent modules via a marker | one cross-package `tests/` directory, no agent conftest | self |
| D10 | Agent tests' config source | autouse fixture patches `host_config.source_from_env` with a file-backed fake (`tests/host_config_fakes.py`) answering through the orchestrator's `answer_sync()` | an HTTP fake orchestrator in every test | about 60 agent tests write `config.yaml` into a temp home; this keeps them valid and still runs the real versioning logic | self |
| D11 | Cache concurrency | synchronous cache: a data lock for reads and a separate lock serialising network syncs; `httpx.Client`, 5 s timeout; the periodic sync runs via `asyncio.to_thread` | async cache | every call site is synchronous (Bedrock client creation, `runtime_environment`, the policy loader) | self |
| D12 | Timing | periodic sync every 300 s; on-use sync when a cached `expires_at` is under 180 s away, at most once per 30 s per credential | 30 s polling | user: 5 min to limit access-log noise, 3 min window; the 30 s spacing stops paginated plugin reads syncing on every call | user, self (spacing) |
| D13 | Startup failure | agent exits 1 (Kiro: writes the startup-error file) when the flag, URL or token is missing, the orchestrator is unreachable, or config is invalid | fall back to defaults or files | the guard forbids a file fallback; defaults would silently drop policy | user |
| D14 | Socket path | `/tmp/archie-agent/agent.sock` (dir 0700), override `ARCHIE_AGENT_SOCKET`, exported to subprocesses | `/run/archie/agent.sock` | `/run` isn't writable by the container user; using it needs an image rebuild | self |
| D15 | Socket server | a second `uvicorn.Server` on the UDS, started in the agent lifespan, signal capture disabled | hand-rolled HTTP on `asyncio.start_unix_server` | reuses Starlette routing; the nested server must not take over SIGTERM from the main one | self |
| D16 | Socket routes | `GET /credentials/{provider}`, `GET /plugins/{plugin_id}/settings` | serve the whole config | `gh` only needs `work_orgs`; keeps `mounts` and the rest out of subprocesses | user |
| D17 | Exec runner reads | `CredentialSource.get()` calls the socket on every call | a 60 s memo in the runner | the agent cache already caches; a socket hit costs about 0.5 ms | self |
| D18 | Policy reload | the policy loader reads the cached config; a `config_error` from the last sync raises, giving the existing reload-failed notice; edits arrive with the next sync | sync before each exec call | the user chose a low request rate | self |
| D19 | Auth-error sync | Bedrock's `_try_refresh_credentials` forces a sync of `bedrock` before rebuilding the client | every client and plugin | only Bedrock has an expiry-retry path today | self |
| D20 | `runtime_environment()` | gains `lookup` (default: the file store); `_runtime_env` passes `host_config.get_credential` and keys its cache on `HostConfig.generation` | file-stat key | the files are no longer the source | user (council), self (key) |
| D21 | Mixed versions | no compatibility path: a new agent started by an old orchestrator exits naming the missing variable; running containers keep their in-memory code | file fallback | contradicts D6/D7; restarting the orchestrator fixes it | self |
| D22 | `scripts/dev` | unsets `ARCHIE_AGENT_CONTAINER` | leave it | manual `scripts/dev uv run archie …` inside a session would otherwise trip the guard | self |

## Requirements

- MUST give each new session a random agent token, store only its SHA-256 hex in
  `sessions.agent_token_hash`, and keep the value out of the `docker run` argv.
  - AC: `prepare_start` argv contains `-e`, `ARCHIE_AGENT_TOKEN` (no `=value`),
    `ARCHIE_AGENT_CONTAINER=1` and `ARCHIE_ORCHESTRATOR_URL=http://host.docker.internal:<profile port>`;
    `run_start` passes the token in `run_container`'s env; the stored row holds
    `sha256(token)`; `migrate()` returns 4.
- MUST reject `POST /api/agent/sync` with 401 `{"error": "unauthorized"}` unless the bearer
  token hashes to a live session's stored hash.
  - AC: no header, an unknown token, and an ended session's token each get 401.
- MUST answer a sync with only the items whose version differs from the request (`null` =
  not held). A version is the first 16 hex of SHA-256 over sorted-key JSON. Unknown
  providers and an unreadable store go to `credential_errors` (store errors name only the
  exception type); an invalid config goes to `config_error`.
  - AC: unchanged versions → empty response; changed entry → returned; absent credential →
    `value: null` with a version; YAML error → message contains `ValueError` and no store
    text.
- MUST start the agent from the orchestrator's config, exiting 1 (Kiro: startup-error file)
  when `ARCHIE_AGENT_CONTAINER` isn't `1`, `ARCHIE_ORCHESTRATOR_URL` or
  `ARCHIE_AGENT_TOKEN` is missing, the orchestrator is unreachable, or config is invalid.
  - AC: lifespan test with an HTTP source pointed at the orchestrator routes uses the served
    model; each failure case raises `SystemExit(1)` with the reason logged.
- MUST remove `ARCHIE_AGENT_TOKEN` from the agent's `os.environ` at startup.
  - AC: after `source_from_env()`, `os.environ` has no `ARCHIE_AGENT_TOKEN` and
    `subprocess_env()` doesn't contain it.
- MUST serve agent-process credential reads from the cache: the first read fetches,
  repeated reads make no request until a sync, and absence is cached.
  - AC: a counting source sees one request for two reads of the same provider, including a
    missing one; Bedrock, OpenAI, Kiro and `runtime_environment` read through it.
- MUST sync a cached OAuth credential whose `expires_at` is under 180 s away when it is
  read, at most once per 30 s per credential, returning the cached value if the sync fails.
  - AC: with a fake clock, a read 100 s before expiry syncs once; a second read 10 s later
    doesn't; a failing source still returns the cached value.
- MUST sync config and every held credential every 300 s, keeping last good values and
  logging a warning on failure.
  - AC: the sync loop with a short interval picks up a changed config and credential; a
    raising source leaves values unchanged.
- MUST keep the last config when a sync returns `config_error`, and show the reload-failed
  notice once on the next policy refresh.
  - AC: `LiveToolPolicy.refresh()` after an erroring sync returns the old snapshot with one
    `tool_policy_reload_failed` notice; after a good sync, one `tool_policy_reloaded`.
- MUST serve `GET /credentials/{provider}` (`{"value": <entry or null>}`) and
  `GET /plugins/{plugin_id}/settings` on a Unix socket at `ARCHIE_AGENT_SOCKET` (default
  `/tmp/archie-agent/agent.sock`, parent dir 0700), exported to subprocesses, and not on
  port 8080.
  - AC: a request over the socket returns the cached entry; unknown provider or plugin → 404;
    the TCP app returns 404 for `/credentials/github`.
- MUST make `CredentialSource.get()` read through the socket and raise when it is
  unreachable.
  - AC: runner-side test against a socket server returns the typed credential; a missing
    socket raises.
- MUST keep the `gh` wrapper's owner-based token choice and `credential-process`'s output
  while reading from the socket; with the socket unreachable `gh` warns and runs `gh-base`
  with its own login, and `credential-process` exits 1 with a message.
  - AC: `tests/test_gh_wrapper.py` and `plugins/aws/tests/test_aws_credential_process.py`
    pass against a fake socket server, with no `credentials.yaml` or `config.yaml` present.
- MUST raise `HostFileAccessError` naming the function, its caller and the replacement when
  `load_nexus_config()` (default path), `store_path()`, `load_store()`, `get_credential()`
  or `runtime_environment()` without `lookup` runs with `ARCHIE_AGENT_CONTAINER=1`;
  `load_nexus_config(path)` still works.
  - AC: `tests/test_host_file_guard.py` covers each, including the transitive
    `runtime_environment()` default.
- MUST fail a static test if non-test code under `agent/` or `plugins/` references
  `load_nexus_config`, `get_credential`, `load_store` or `store_path` from `archie_shared`,
  or a script under `container/bin/` or `plugins/*/bin/` mentions `config.yaml` or
  `credentials.yaml`.
  - AC: the test passes on the change and fails when one reference is added back (checked
    by hand once).
- MUST stop naming `config.yaml` and `credentials.yaml` in the system prompt.
  - AC: `rg 'config.yaml|credentials.yaml' persona/prompts` finds nothing.
- MUST document the flow: architecture (env vars, socket, `/api/agent/sync`),
  auth-credentials (runtime env, `gh`, AWS), config and plugins (policy timing).
  - AC: `rg -n 'before (each|every) exec call' docs` finds nothing; the API table lists
    `POST /api/agent/sync`.

Out of scope: removing the files from the mount; authenticating orchestrator→agent calls;
user-side auth (web login, CLI token, Origin checks); env-var projection of `set_env`
credentials; `archie.db`, brain, `agent/` and `persona/` mounts; limiting credentials to
enabled plugins; push invalidation; auth-error syncs outside Bedrock.

## Design

### Overview

The orchestrator gains one authenticated route that answers "here are my versions; what
changed?". The agent holds config and credentials in a `HostConfig` cache, created at
startup from the orchestrator, refreshed every 5 minutes and on misses, and read by every
in-process consumer. Subprocesses read the same cache through a Unix-socket app. A guard
in `shared` makes any leftover file read in a container raise.

### Code structure

- `shared/src/archie_shared/agent_sync.py` (new): `SyncRequest`, `SyncItem`,
  `SyncResponse` structs and `version_of(value) -> str`.
- `shared/src/archie_shared/config.py`: `AGENT_CONTAINER_ENV`, `HostFileAccessError`,
  `guard_host_files(what, replacement)`.
- `shared/src/archie_shared/schemas.py`, `credentials/store.py`: call the guard.
  `credentials/runtime.py`: `runtime_environment(config, lookup=get_credential)`.
- `orchestrator/src/archie_orchestrator/agent_api.py` (new): `answer_sync(request, *,
  load_config, load_store) -> SyncResponse` (pure) and the `POST /sync` route with bearer
  auth; mounted as `Mount("/agent", routes=agent_api.routes)` inside `api.routes`.
- `orchestrator/.../store.py`: migration 4, `insert(record, *, agent_token_hash=None)`,
  `session_for_token_hash(hash) -> str | None`.
- `orchestrator/.../lifecycle.py`: `PreparedStart.agent_token` (default `""`), the three
  `-e` flags, `run_start` passes the token env. `docker.py`: `run_container(cmd, env=None)`.
- `orchestrator/.../tracker.py` `create()`: stores the hash with the row.
- `agent/src/archie_agent/host_config.py` (new): `Source` protocol, `HttpSource`,
  `source_from_env()`, `HostConfig`, module `current()/install()/reset()`,
  `get_config()`, `get_credential()`, `sync_loop()`.
- `agent/src/archie_agent/local_api.py` (new): socket Starlette app, `socket_path()`,
  `serve_local()` (server task), and `LocalClient` for subprocess callers.
- Call sites: `app.py`, `tool_policy_live.py`, `skills.py`, `exec/tools/_runtime_env.py`,
  `exec/plugin.py`, `llm/bedrock.py`, `llm/bedrock_openai.py`, `llm/openai_api.py`,
  `kiro/harness.py`.
- Scripts: `container/bin/gh`, `plugins/aws/bin/credential-process`; `scripts/dev`.
- Tests: `tests/host_config_fakes.py` (new: `FileSource`, `SocketServer` test helper),
  `tests/conftest.py` (autouse fixtures), new `tests/test_agent_sync_api.py`,
  `tests/test_host_config.py`, `tests/test_local_api.py`, `tests/test_host_file_guard.py`.

### Data model

- `sessions.agent_token_hash TEXT NULL`, unique index; not in `COLUMNS` (not part of
  `SessionDescriptor`); rows from before migration 4 stay null.
- `SyncRequest {config: str | None, credentials: dict[str, str | None]}`.
- `SyncItem {version: str, value: Any}`; `SyncResponse {config: SyncItem | None,
  config_error: str | None, credentials: dict[str, SyncItem], credential_errors: dict[str, str]}`.
  Config `value` is `msgspec.to_builtins(NexusConfig)`; a credential `value` is the raw
  store entry or null.
- `HostConfig` state: config + version, config_error, `{name: (version, entry)}`, a
  `generation` int bumped on every change, last early-sync time per name.

### Data flow

- **Startup:** `source_from_env()` checks the flag, reads the URL, pops the token →
  `HostConfig.start()` syncs with `config: null` → install → start the socket server and the
  sync loop. Any failure → exit 1.
- **Read (agent process):** hit → return; miss → sync `{name: null}` → store (absence
  too) → return. OAuth near expiry → targeted sync (spaced) → return cached.
- **Read (subprocess):** `GET /credentials/x` on the socket → `HostConfig` read (in a
  thread) → JSON.
- **Periodic:** every 300 s, sync config version + all held versions; apply changes.

### Error handling

- Orchestrator unreachable or 5xx during a periodic/early sync → warning logged, last good
  values kept. During a miss → `HostConfigError` (callers see their usual "credentials
  unavailable" path). At startup → exit 1.
- `config_error` → config kept, error stored, surfaced by the policy loader.
- A credential error for one name → that read raises `ValueError` (as an invalid store entry
  does today); others unaffected.
- Socket route failure → 503 `{"error": ...}`; scripts treat non-2xx like an unreadable file.

### Config / infra

- Env vars set by the orchestrator: `ARCHIE_AGENT_CONTAINER=1`, `ARCHIE_ORCHESTRATOR_URL`,
  `ARCHIE_AGENT_TOKEN`. Set by the agent: `ARCHIE_AGENT_SOCKET`.
- Migration 4 on `archie.db` (additive column and index).
- No image rebuild: the scripts and `agent/` are mounted.

## Milestones

### [ ] M1 — Agent starts from orchestrator-served config

- **Approach:** add `agent_sync.py`; migration 4 and store methods (follow `_migration_3`
  in `store.py`); token generation in `prepare_start` (`secrets.token_urlsafe(32)`),
  `run_container(cmd, env=None)`, hash stored in `tracker.create()`; `agent_api.py` with
  `answer_sync()` and the route (auth via `request.app.state.tracker`: store lookup in a
  thread, then `tracker.live()`); `host_config.py` with `HttpSource` and `HostConfig`
  (config only needed here, but `answer_sync` handles credentials too); `app.py` lifespans
  (native and Kiro) build config from `HostConfig`; `tool_policy_live.py` and `skills.py`
  read `host_config.get_config()`. Add `tests/host_config_fakes.py` `FileSource` and the
  autouse fixtures in `tests/conftest.py` (patch `source_from_env`, reset the installed
  cache). Update tests that patch `run_container` with `captured.extend`.
  ⚠️ `HttpSource` takes an injectable `httpx.Client` so tests can pass a Starlette
  `TestClient` over the orchestrator routes.
- **Test seam:** `answer_sync()`; the `/api/agent/sync` route through `TestClient`;
  `HostConfig` + `HttpSource` against that route; the agent lifespan.
- **Edge cases:** bad token / ended session → 401; invalid host config at startup → exit 1;
  missing env var → exit 1 naming it.
- **Deliverable:** an agent started with a valid token runs with the orchestrator's config.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_agent_sync_api.py
  tests/test_host_config.py tests/test_session_store.py tests/test_orchestrator_lifecycle.py
  tests/test_ws_integration.py tests/test_tool_policy_live.py` passes, then the full suite.
- **Outcome:**

### [ ] M2 — Credentials through the pull-through cache

- **Approach:** `HostConfig.get_credential()` (miss fetch, cached absence, typed with
  `CREDENTIAL_TYPES` like `store.get_credential`), OAuth early sync with spacing, a
  `refresh(name)` for Bedrock; module `get_credential()`. Switch Bedrock (both paths),
  Bedrock-OpenAI, OpenAI, `kiro/harness.resolve_api_key`; `runtime_environment(lookup=)`
  and `_runtime_env` keyed on `generation`. Update tests that relied on per-call re-reads
  (`tests/test_openai_api.py`, `tests/test_runtime_env_live.py`) to sync first.
- **Test seam:** `HostConfig` with a counting source and fake clock; `runtime_env()`.
- **Deliverable:** agent-process credential reads come from the cache.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_host_config.py
  tests/test_openai_api.py tests/test_runtime_env_live.py tests/test_credentials_runtime.py
  tests/test_kiro_harness.py` passes, then the full suite.
- **Outcome:**

### [ ] M3 — Periodic sync and policy errors

- **Approach:** `sync_loop(interval=300)` started as a task in both lifespans and cancelled
  at shutdown; `HostConfig.sync()` sends the config and all held versions; the policy
  loader raises `ConfigError(config_error)` when set. Update the live-reload tests in
  `tests/test_tool_policy_live.py` and `tests/test_tool_policy_live_exec.py` to sync before
  expecting a change.
- **Test seam:** `sync_loop` with a short interval; `LiveToolPolicy.refresh()`.
- **Deliverable:** host edits reach running sessions within one sync.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_host_config.py
  tests/test_tool_policy_live.py tests/test_tool_policy_live_exec.py` passes, then the full
  suite.
- **Outcome:**

### [ ] M4 — Subprocesses read through the agent socket

- **Approach:** `local_api.py` app and server (subclass `uvicorn.Server`, override
  `capture_signals` with a null context), started after `HostConfig` in both lifespans,
  sets `ARCHIE_AGENT_SOCKET`; `LocalClient` (`httpx.Client` with `HTTPTransport(uds=...)`)
  used by `CredentialSource.get()`; rewrite `container/bin/gh` and
  `plugins/aws/bin/credential-process` reads as `curl -sf --unix-socket "$sock"`; rewrite
  their tests against a threaded `socketserver.UnixStreamServer` helper in
  `tests/host_config_fakes.py` (the AWS plugin tests import it by path or get a local
  copy). Autouse fixture sets `ARCHIE_AGENT_SOCKET` to a short temp path.
- **Test seam:** the socket app over a real UDS; `CredentialSource.get()`; the two scripts
  as subprocesses.
- **Deliverable:** `gh`, AWS and exec plugins get credentials without the files.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_local_api.py
  tests/test_gh_wrapper.py plugins/aws/tests` passes, then the full suite.
- **Outcome:**

### [ ] M5 — Fail-loud guard, prompt and docs

- **Approach:** `guard_host_files()` and its calls; root conftest clears the flag;
  `scripts/dev` unsets it; `tests/test_host_file_guard.py` (runtime cases and the static
  scan); remove the file names from `persona/prompts/environment.md`; update
  `docs/architecture.md`, `docs/auth-credentials.md`, `docs/config.md`, `docs/plugins.md`.
- **Test seam:** the shared readers under the flag; a file scan.
- **Deliverable:** a missed call site raises in a container; docs match.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_host_file_guard.py`
  passes; `rg 'config.yaml|credentials.yaml' persona/prompts` and
  `rg -n 'before (each|every) exec call' docs` print nothing; full suite and ruff pass.
- **Outcome:**

## Deviations

## Risks

- The nested uvicorn server's interaction with the main server's signal handling is
  unverified until M4; if it misbehaves, fall back to `asyncio.start_unix_server` (D15).
- Policy edits now take up to 5 minutes to reach a running session (D18).
- A sync miss blocks the calling thread for up to the 5 s timeout when the orchestrator is
  down; on the event-loop thread that stalls the agent briefly.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups
