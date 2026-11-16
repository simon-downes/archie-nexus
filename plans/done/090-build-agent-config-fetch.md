# 090 — Agent config and credentials from the orchestrator

Status: done
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
  reader runs in a container; test setup clears the flag and targeted tests set it; the
  agent refuses to start without the flag.
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
| D9 | Test default for the flag | the repo-root `conftest.py` (which covers `tests/` and `plugins/`, `testpaths`) clears it and pins `ARCHIE_AGENT_SOCKET` to a short temp path; guard and host-config tests set the flag | on by default for agent modules via a marker | tests and plugin tests share the root conftest, whose autouse `_isolated_nexus_home` is the existing pattern; a session's inherited socket path must never reach the live agent | self |
| D10 | Agent tests' config source | root-conftest autouse fixture patches `host_config.source_from_env` with a file-backed fake (`FileSource`, defined in the root `conftest.py`) answering through the orchestrator's `answer_sync()`, and resets the installed cache | an HTTP fake orchestrator in every test | about 60 agent tests write `config.yaml` into a temp home; this keeps them valid and still runs the real versioning logic | self |
| D11 | Cache concurrency | synchronous cache: a data lock for reads and a separate lock serialising network syncs; `httpx.Client`, 5 s timeout; the periodic sync runs via `asyncio.to_thread` | async cache | every call site is synchronous (Bedrock client creation, `runtime_environment`, the policy loader) | self |
| D12 | Timing | periodic sync every 300 s; on-use sync when a cached `expires_at` is under 180 s away, at most once per 30 s per credential | 30 s polling | user: 5 min to limit access-log noise, 3 min window; the 30 s spacing stops paginated plugin reads syncing on every call | user, self (spacing) |
| D13 | Startup failure | agent exits 1 (Kiro: writes the startup-error file) when the flag, URL or token is missing, the orchestrator is unreachable, or config is invalid | fall back to defaults or files | the guard forbids a file fallback; defaults would silently drop policy | user |
| D14 | Socket path | `/tmp/archie-agent/agent.sock` (dir 0700), override `ARCHIE_AGENT_SOCKET`, exported to subprocesses | `/run/archie/agent.sock` | `/run` isn't writable by the container user; using it needs an image rebuild | self |
| D15 | Socket server | a second `uvicorn.Server` on the UDS, started in the agent lifespan (nested-server precedent: `kiro/mcp_server.py`), with `capture_signals` overridden to a null context | hand-rolled HTTP on `asyncio.start_unix_server` | reuses Starlette routing; uvicorn 0.50's `capture_signals` swaps the process's SIGTERM handler while a server runs, so a nested server would take SIGTERM and stop first | self |
| D16 | Socket routes | `GET /credentials/{provider}`, `GET /plugins/{plugin_id}/settings` | serve the whole config | `gh` only needs `work_orgs`; keeps `mounts` and the rest out of subprocesses | user |
| D17 | Plugin credential reads | in the exec runner, `CredentialSource.get()` calls the socket on every call (`LocalClient`, 5 s timeout); plugin startup hooks, which run in the agent process, get `CredentialSource(pid, loader=...)` reading `host_config` directly | socket everywhere, or a 60 s memo in the runner | the agent cache already caches (a socket hit costs about 0.5 ms); a hook calling the socket from the event-loop thread would block the server that answers it | self |
| D18 | Policy reload | the policy loader reads the cached config; a `config_error` from the last sync raises, giving the existing reload-failed notice; edits arrive with the next sync | sync before each exec call | the user chose a low request rate | self |
| D19 | Auth-error sync | Bedrock's `_try_refresh_credentials` forces a sync of `bedrock` before rebuilding the client | every client and plugin | only Bedrock has an expiry-retry path today | self |
| D20 | `runtime_environment()` | gains `lookup` (default: the file store); `_runtime_env` passes `host_config.get_credential` and keys its cache on `HostConfig.generation` | file-stat key | the files are no longer the source | user (council), self (key) |
| D21 | Mixed versions | no compatibility path: a new agent started by an old orchestrator exits naming the missing variable; sessions running when the checkout is updated must be restarted (their mounted scripts and runner code change, but they have no socket) | file fallback | contradicts D6/D7; restarting the orchestrator and sessions once fixes it | self |
| D22 | `scripts/dev` | unsets `ARCHIE_AGENT_CONTAINER` | leave it | manual `scripts/dev uv run archie …` inside a session would otherwise trip the guard | self |
| D23 | Empty token | `PreparedStart.agent_token` defaults to `""`; no token stores `NULL`, and the route rejects an empty bearer | hash whatever is given | `tests/session_fakes.py` builds `PreparedStart` without a token; hashing `""` would collide on the unique index | self |
| D24 | Failed sync after startup | a periodic or early sync that fails (unreachable, 401, 5xx, or `credential_errors` for a held name) logs a warning and keeps the held value; a miss that fails raises `HostConfigError` | replace held values with errors | keeps last-good behaviour (`_runtime_env._last_good`, `LiveToolPolicy`); a mid-edit invalid `credentials.yaml` mustn't break running sessions | self |
| D25 | Settings route | `GET /plugins/{plugin_id}/settings` returns `resolved_settings(pid, config.plugins, manifests)` for any manifest id, enabled or not; no manifest → 404 | raw `config.plugins.<id>.settings`, or only enabled plugins | manifest defaults apply as in the runner context; the `gh` wrapper reads `work_orgs` today whether or not the plugin is enabled | self |
| D26 | `host_config.current()` with nothing installed | builds lazily from `source_from_env()` and starts it | raise | the exec runner and stray callers still fail loudly (no token); tests calling `load_policy_snapshot()` or `discover_skills()` without a lifespan work through the patched factory | self |
| D28 | Error types | `HostConfigError(ValueError)` for transport, 401, 5xx and per-name errors on a miss; a `config_error` at start (or lazy start) raises `ConfigError(<message>)` | one new exception type everywhere | existing handlers catch these: `skills._enabled_plugin_ids` (`ConfigError`), `resolve_api_key` (`KeyError, ValueError`), `_runtime_env._compute` (quotes `ConfigError` messages), plugins (`Exception`) | self |
| D29 | Who handles sync failures | `HostConfig.sync()` raises `HostConfigError` on transport, 401 and 5xx; it applies a response itself, dropping `credential_errors` for held names with a warning and clearing `config_error` when the response has none; `sync_loop` and the early OAuth sync catch and log; a miss lets the error propagate | handling everything in the loop | one place applies responses; callers choose whether a failure is fatal | self |
| D27 | Orchestrator URL | `http://host.docker.internal:{get_profile(config.orchestrator).port}` (the call `archie serve` uses) | hard-coded 7600, `ARCHIE_HOST` | follows a configured default-profile port | self |

## Requirements

- MUST give each new session a random agent token, store only its SHA-256 hex in
  `sessions.agent_token_hash`, and keep the value out of the `docker run` argv.
  - AC: `prepare_start` argv contains `-e`, `ARCHIE_AGENT_TOKEN` (no `=value`),
    `ARCHIE_AGENT_CONTAINER=1` and
    `ARCHIE_ORCHESTRATOR_URL=http://host.docker.internal:<get_profile(config.orchestrator).port>`;
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
  logging a warning on failure (unreachable, 401, 5xx, or a per-name error for a held name).
  - AC: the sync loop with a short interval picks up a changed config and credential; a
    raising source, and a response with `credential_errors` for a held name, leave values
    unchanged.
- MUST keep the last config when a sync returns `config_error`, and show the reload-failed
  notice once on the next policy refresh.
  - AC: `LiveToolPolicy.refresh()` after an erroring sync returns the old snapshot with one
    `tool_policy_reload_failed` notice; after a good sync, one `tool_policy_reloaded`.
- MUST serve `GET /credentials/{provider}` (`{"value": <entry or null>}`) and
  `GET /plugins/{plugin_id}/settings` on a Unix socket at `ARCHIE_AGENT_SOCKET` (default
  `/tmp/archie-agent/agent.sock`, parent dir 0700), exported to subprocesses, and not on
  port 8080.
  - AC: a request over the socket returns the cached entry; settings are
    `resolved_settings()` (manifest defaults applied); unknown provider or plugin manifest →
    404; the TCP app returns 404 for `/credentials/github`.
- MUST make `CredentialSource.get()` read through the socket and raise when it is
  unreachable.
  - AC: runner-side test against a socket server returns the typed credential; a missing
    socket raises.
- MUST keep the `gh` wrapper's owner-based token choice and `credential-process`'s output
  while reading from the socket; with the socket unreachable `gh` warns and runs `gh-base`
  with its own login, and `credential-process` exits 1 with a message.
  - AC: `tests/test_gh_wrapper.py` and `plugins/aws/tests/test_aws_credential_process.py`
    pass against a fake socket server, with no `credentials.yaml` or `config.yaml` present.
- MUST raise `HostFileAccessError` when `load_nexus_config()` (default path),
  `store_path()`, `load_store()`, `get_credential()` or `runtime_environment()` without
  `lookup` runs with `ARCHIE_AGENT_CONTAINER=1`. The message names the guarded reader
  (`load_nexus_config` or `store_path`), the first calling frame outside `archie_shared`
  (`file:line`), and `archie_agent.host_config` as the replacement.
  `load_nexus_config(path)` still works.
  - AC: `tests/test_host_file_guard.py` covers each, including the transitive
    `runtime_environment()` default, and checks the caller in the message is the test file.
- MUST fail a static test if non-test code under `agent/` or `plugins/` references
  `load_nexus_config`, `get_credential`, `load_store` or `store_path` from `archie_shared`,
  or a script under `container/bin/` or `plugins/*/bin/` mentions `config.yaml` or
  `credentials.yaml`.
  - AC: the scanner function returns no hits on the repo, and returns a hit for a fixture
    source string that imports `get_credential` from `archie_shared.credentials` and for a
    fixture script that names `credentials.yaml`.
- MUST stop naming `config.yaml` and `credentials.yaml` in the system prompt.
  - AC: `rg 'config.yaml|credentials.yaml' persona/prompts` finds nothing.
- MUST document the flow: architecture (env vars, socket, `/api/agent/sync`),
  auth-credentials (runtime env, `gh`, AWS), config and plugins (policy timing), and the
  plugin READMEs that describe file reads (`plugins/github/README.md`,
  `plugins/github/plugin.yaml` comment, `plugins/aws/README.md`, `plugins/scalr/README.md`,
  `plugins/cloudflare/README.md`).
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
  `guard_host_files(reader)` (the replacement, `archie_agent.host_config`, is in its message).
- `shared/src/archie_shared/credentials/models.py`: `credential_type_for()` and
  `to_credential()`, the typed conversion shared by the store, `host_config` and `local_api`.
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
- Tests: the repo-root `conftest.py` gains `FileSource`, the autouse fixtures (flag
  cleared, socket path pinned, `source_from_env` patched, cache reset) and a
  `fake_agent_socket` fixture (threaded `socketserver.UnixStreamServer` serving given JSON
  routes) used by `tests/test_gh_wrapper.py` and `plugins/aws/tests/`; new
  `tests/test_agent_sync_api.py`, `tests/test_host_config.py`, `tests/test_local_api.py`,
  `tests/test_host_file_guard.py`.

### Data model

- `sessions.agent_token_hash TEXT NULL`, unique index; not in `COLUMNS` (not part of
  `SessionDescriptor`); rows from before migration 4 stay null.
- `SyncRequest {config: str | None, credentials: dict[str, str | None]}`.
- `SyncItem {version: str, value: Any}`; `SyncResponse {config: SyncItem | None,
  config_error: str | None, credentials: dict[str, SyncItem], credential_errors: dict[str, str]}`.
  Config `value` is `msgspec.to_builtins(NexusConfig)`; a credential `value` is the raw
  store entry or null.
- `HostConfig` state: config + version, `config_error` (set by a response that has one,
  cleared by a response that doesn't), `{name: (version, entry)}`, a `generation` int
  bumped on every change, last early-sync time per name.

### Data flow

- **Startup (both lifespans):** `source_from_env()` checks the flag, reads the URL, pops
  the token → `HostConfig.start()` syncs with `config: null` → install → start the socket
  server and set `os.environ["ARCHIE_AGENT_SOCKET"]` (so `subprocess_env()` and the Kiro
  harness's env copy inherit it) → `run_startup_hooks` → harness construction/start → sync
  loop. Any failure before the harness → exit 1.
- **Read (agent process):** hit → return; miss → sync `{name: null}` → store (absence
  too) → return. OAuth near expiry → targeted sync (spaced) → return cached.
- **Read (subprocess):** `GET /credentials/x` on the socket → `HostConfig` read (in a
  thread) → JSON.
- **Periodic:** every 300 s, sync config version + all held versions; apply changes.

### Error handling

- Orchestrator unreachable, 401 or 5xx during a periodic/early sync → warning logged, last
  good values kept (D24). During a miss → `HostConfigError` (callers see their usual
  "credentials unavailable" path). At startup → exit 1.
- `config_error` → config kept, error stored, surfaced by the policy loader.
- A credential error for a name not held (a miss) → that read raises `ValueError` (as an
  invalid store entry does today); for a held name → warning, value kept.
- Socket route failure → 503 `{"error": ...}`; scripts treat non-2xx like an unreadable file.

### Non-functional

- Token: 32 random bytes (`secrets.token_urlsafe`), stored only as SHA-256 hex (D1), never in
  argv (D2), removed from the agent's `os.environ`. It stays readable in `/proc/1/environ`
  and in `docker exec` shells, which the threat model (incidental exposure) accepts.
- Socket: parent directory mode 0700 in the container's `/tmp`, GET-only routes. It serves
  every provider's credential (OAuth tokens and `bedrock` included) to any process running
  as the container user, the model's shell among them. That is deliberate access, which the
  threat model accepts; D16 keeps only the rest of the config out of subprocesses.
- Request volume: one sync per session every 300 s, plus misses and early OAuth syncs.

### Config / infra

- Env vars set by the orchestrator: `ARCHIE_AGENT_CONTAINER=1`, `ARCHIE_ORCHESTRATOR_URL`,
  `ARCHIE_AGENT_TOKEN`. Set by the agent: `ARCHIE_AGENT_SOCKET`.
- Migration 4 on `archie.db` (additive column and index).
- No image rebuild: the scripts and `agent/` are mounted.

## Milestones

### [x] M1 — Agent starts from orchestrator-served config

- **Approach:** add `agent_sync.py`; migration 4 and store methods (follow `_migration_3`
  in `store.py`); token generation in `prepare_start` (`secrets.token_urlsafe(32)`),
  `run_container(cmd, env=None)`, hash stored in `tracker.create()` (NULL without a
  token, D23); the `wait_for_ready` "re-run without --rm" hint notes that
  `ARCHIE_AGENT_TOKEN` must be exported; `agent_api.py` with `answer_sync()` and the route
  (auth via `request.app.state.tracker`: store lookup in a thread, then `tracker.live()`);
  `host_config.py` with `source_from_env()` (flag check, URL, token popped), `HttpSource`,
  `HostConfig.start()`/`sync()` for config and `config_error` (D28, D29), lazy `current()`
  (D26); `PreparedStart.agent_token` uses `field(repr=False)`; `run_container` merges its
  `env` into `os.environ` (the docker CLI needs `PATH`, `HOME`, `DOCKER_HOST`);
  `app.py` lifespans (native and Kiro) build config from `HostConfig`;
  `tool_policy_live.py` (loader raises `ConfigError(config_error)` when set) and
  `skills.py` read `host_config`. Add `FileSource` and the autouse fixtures to the root
  `conftest.py`; `app.py` calls `host_config.source_from_env()` as a module attribute so
  the patch applies, and the conftest keeps the original as a `real_source_from_env`
  fixture for the tests of the real function. Update the live-reload tests in
  `tests/test_tool_policy_live.py`, `tests/test_tool_policy_live_exec.py` and
  `tests/test_plugin_enabled.py` (skills) to call `host_config.current().sync()` after
  editing `config.yaml`; the `migrate(...) == 3` assertions in
  `tests/test_session_store.py` to 4; and the tests that patch `run_container` with
  `captured.extend`. Tests that set `ARCHIE_AGENT_CONTAINER=1` set it only around
  `source_from_env()` (and, in M5, the guard calls): `FileSource` and the in-process
  orchestrator routes read the host files, which the M5 guard forbids under the flag.
  ⚠️ `HttpSource` takes an injectable `httpx.Client` so tests can pass a Starlette
  `TestClient` over the orchestrator routes.
- **Test seam:** `answer_sync()`; the `/api/agent/sync` route through `TestClient`;
  `HostConfig` + `HttpSource` against that route; the agent lifespan.
- **Edge cases:** bad or empty token / ended session → 401; invalid host config at startup
  → exit 1; missing env var → exit 1 naming it; `config_error` after startup → one
  reload-failed notice, old policy kept.
- **Deliverable:** an agent started with a valid token runs with the orchestrator's config.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_agent_sync_api.py
  tests/test_host_config.py tests/test_session_store.py tests/test_orchestrator_lifecycle.py
  tests/test_ws_integration.py tests/test_tool_policy_live.py
  tests/test_tool_policy_live_exec.py tests/test_plugin_enabled.py tests/test_kiro_harness.py
  tests/test_plugin_loader.py` passes, then the full suite.
- **Outcome:** `POST /api/agent/sync` (`agent_api.py`, `answer_sync`), migration 4 with
  `insert(..., agent_token_hash=)` / `session_for_token_hash`, per-session token in
  `prepare_start` (name-only `-e`, value via `run_container(env=)`), and
  `archie_agent.host_config` (`HttpSource`, `source_from_env`, `HostConfig`, lazy
  `current()`). Both lifespans, the policy loader and skills read it. Evidence:
  `tests/test_agent_sync_api.py` and `tests/test_host_config.py` (new, including the
  agent `/status` model coming from the orchestrator route), focused run 406 passed; full
  suite 5572 passed; `ruff check .` clean. The `migrate == 3` and `run_container` test
  doubles were updated, and `test_session_store.py` gained a migration 4 test.

### [x] M2 — Credentials through the pull-through cache

- **Approach:** `HostConfig.get_credential()` (miss fetch, cached absence, typed with
  `CREDENTIAL_TYPES` like `store.get_credential`), `sync()` extended to send and apply
  every held credential version (D29), OAuth early sync with spacing, a `refresh(name)`
  for Bedrock; module `get_credential()`. Switch Bedrock (both paths),
  Bedrock-OpenAI, OpenAI, `kiro/harness.resolve_api_key`; `runtime_environment(lookup=)`
  and `_runtime_env` keyed on `generation`. Update tests that relied on per-call re-reads
  (`tests/test_openai_api.py`, `tests/test_runtime_env_live.py`) to sync first.
- **Test seam:** `HostConfig` with a counting source and fake clock; `runtime_env()`.
- **Deliverable:** agent-process credential reads come from the cache.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_host_config.py
  tests/test_openai_api.py tests/test_runtime_env_live.py tests/test_credentials_runtime.py
  tests/test_kiro_harness.py` passes, then the full suite.
- **Outcome:** `HostConfig.credential_entry()`/`get_credential()` (miss fetch, cached
  absence, typed like the store), `sync(names)` sending every held version and keeping held
  values on per-name errors, the 180 s / 30 s early sync, and module `get_credential()` /
  `refresh()`. Bedrock (both paths, refresh via `host_config.refresh`), Bedrock-OpenAI,
  OpenAI and `resolve_api_key` read through it; `runtime_environment(config, lookup=)` and
  `_runtime_env` keyed on `(id(host), generation)`. Deviation: the exec runner now calls
  `_runtime_env.inherit_environment()` (see Deviations). Evidence: `tests/test_host_config.py`
  28 passed (counting source, fake clock, Bedrock/OpenAI/Kiro reads); focused run 106
  passed; full suite 5585 passed; `ruff check .` clean.

### [x] M3 — Periodic sync

- **Approach:** `sync_loop(hc, interval=300)` started as a task at the end of both
  lifespans and cancelled at shutdown; it runs `HostConfig.sync()` (config version plus all
  held versions) in a thread and applies D24 on failure.
- **Test seam:** `sync_loop` with a short interval and a fake source.
- **Deliverable:** host edits reach running sessions within one sync without a request
  from the session.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_host_config.py
  tests/test_ws_integration.py` passes, then the full suite.
- **Outcome:** `host_config.sync_loop()` (default `SYNC_INTERVAL` 300 s, read at call
  time) runs `HostConfig.sync` in a thread, logging `HostConfigError` as a warning and
  anything else with a traceback, and never stops on a failure. Both lifespans wrap their
  `yield` in `_periodic_sync()`, which cancels the task at shutdown. Evidence: loop tests
  (edits arrive, failures keep values and the loop runs on, an unexpected error is
  survived) and an agent-lifespan test with a 0.02 s interval; focused run 88 passed; full
  suite 5589 passed; `ruff check .` clean.

### [x] M4 — Subprocesses read through the agent socket

- **Approach:** `local_api.py` app and server (subclass `uvicorn.Server`, override
  `capture_signals` with a null context), started after `HostConfig` in both lifespans,
  sets `ARCHIE_AGENT_SOCKET`; `LocalClient` (`httpx.Client` with `HTTPTransport(uds=...)`)
  used by `CredentialSource.get()` when no `loader` is given; `run_startup_hooks` passes
  a `host_config` loader (D17); rewrite `container/bin/gh` and
  `plugins/aws/bin/credential-process` reads as `curl -sf --unix-socket "$sock"`; rewrite
  `tests/test_gh_wrapper.py`, `plugins/aws/tests/test_aws_credential_process.py` and
  `tests/test_plugin_sdk.py::test_credential_source_reads_store_every_call` and
  `::test_credential_source_only_reads_its_own_plugin` against the root-conftest
  `fake_agent_socket` fixture.
- **Test seam:** the socket app over a real UDS; `CredentialSource.get()`; the two scripts
  as subprocesses.
- **Deliverable:** `gh`, AWS and exec plugins get credentials without the files.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_local_api.py
  tests/test_gh_wrapper.py tests/test_plugin_sdk.py tests/test_plugin_loader.py
  plugins/aws/tests` passes, then the full suite.
- **Outcome:** `archie_agent.local_api`: Starlette app with `GET /credentials/{provider}`
  and `GET /plugins/{plugin_id}/settings`, served by `serve_local()` (a uvicorn server
  with `capture_signals` disabled) in a 0700 directory; it sets `ARCHIE_AGENT_SOCKET`.
  `lifespan` is now: host config, then `serve_local()`, then `_backend_lifespan` (native or
  Kiro). `CredentialSource.get()` uses `local_api.get_credential`; startup hooks get a
  `host_config` loader. `container/bin/gh` and `credential-process` read via
  `curl --unix-socket` (shellcheck clean); a settings error still falls back to the
  personal token, as an unreadable `config.yaml` did. Root `conftest.py` pins the socket
  path per test and adds `fake_agent_socket`. Evidence: focused run 234 passed
  (`tests/test_local_api.py`, rewritten `tests/test_gh_wrapper.py` and
  `plugins/aws/tests/test_aws_credential_process.py`, `test_plugin_sdk.py`, a hook test in
  `test_plugin_loader.py`); full suite 5598 passed; `ruff check .` clean; no
  `/tmp/archie-sock-*` left behind.

### [x] M5 — Fail-loud guard, prompt and docs

- **Approach:** `guard_host_files()` and its calls; `scripts/dev` unsets the flag; `tests/test_host_file_guard.py` (runtime cases, and a scanner
  function tested on the repo and on fixture strings); remove the file names from
  `persona/prompts/environment.md`; update `docs/architecture.md`,
  `docs/auth-credentials.md`, `docs/config.md`, `docs/plugins.md` and the plugin READMEs
  named in the requirement.
- **Test seam:** the shared readers under the flag; a file scan.
- **Deliverable:** a missed call site raises in a container; docs match.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test -q tests/test_host_file_guard.py`
  passes; `rg 'config.yaml|credentials.yaml' persona/prompts` and
  `rg -n 'before (each|every) exec call' docs` print nothing; full suite and ruff pass.
- **Outcome:** `guard_host_files()` and `HostFileAccessError` in `shared/config.py`, called
  by `load_nexus_config()` (default path) and `store_path()`; the message names the reader,
  the first caller outside `archie_shared` and `archie_agent.host_config`. `scripts/dev`
  unsets `ARCHIE_AGENT_CONTAINER`. `tests/test_host_file_guard.py` (17 tests): each reader,
  the transitive `runtime_environment()` default, an explicit path, an AST scanner over
  agent and plugin code (no hits) tested on five fixture forms and an allowed form, and the
  script check. `environment.md` no longer names the files. Docs: new "Agent config and
  credentials" section and API row in `architecture.md`; `auth-credentials.md`, `config.md`,
  `plugins.md`, the github/aws/scalr/cloudflare/kiro READMEs, the github manifest comment
  and the google and aws skills. Evidence: both `rg` ACs print nothing; `ruff check .` and
  `ruff format --check` clean; shellcheck clean; full suite 5615 passed.

## Deviations

- M2: shells started inside exec (the runner's `shell` helper) called `subprocess_env()`,
  which would now try to build a `HostConfig` in the runner and fail without a token. The
  runner calls `_runtime_env.inherit_environment()` at startup, so those shells reuse the
  environment the agent built for the runner (the same values as before, without the
  warning). Behaviour for the model is unchanged; tested in
  `tests/test_runtime_env_live.py::test_shells_inside_exec_inherit_the_runners_environment`.

## Risks

- The nested uvicorn server's shutdown order is unverified until M4; if overriding
  `capture_signals` misbehaves, fall back to `asyncio.start_unix_server` (D15).
- Supported container runtimes: colima (verified reachable) and Docker Desktop (host
  loopback is reachable through `host.docker.internal`, not tested here). On native Linux
  Docker, `host-gateway` is the bridge IP and a loopback-bound orchestrator is unreachable,
  so every agent would exit 1 at startup (D13). Making the bind configurable for that case
  is a follow-up.
- Policy edits now take up to 5 minutes to reach a running session (D18).
- A sync miss blocks the calling thread for up to the 5 s timeout when the orchestrator is
  down; on the event-loop thread that stalls the agent briefly. A failed projected-env
  compute is retried every 30 s, so while the orchestrator is down a shell command can wait
  that long once per 30 s.
- Sessions running when the checkout is updated get the new scripts and runner code (they
  are mounted) but have no agent socket: `gh` runs without a token, AWS `credential-process`
  fails and exec plugin credential reads fail until the session is restarted. Documented as
  an upgrade step in `docs/architecture.md` and the PR.
- `ARCHIE_ORCHESTRATOR_URL` follows the default profile port in the config at session
  start, not the port `archie serve` bound; editing the port without restarting the
  orchestrator breaks new sessions, as it already breaks the CLI.

## Review

- Plan review: one pass. Fixed the ❌ (M1 switched the policy loader but its live-reload
  tests only got updated in M3: `sync()`, `config_error` and those test updates moved into
  M1; M3 is now just the loop). Fixed all ⚠️: token pop assigned to M1; D24 for failed
  syncs of held values; D9/D10 moved fixtures to the repo-root `conftest.py` (which covers
  `plugins/`) and pin `ARCHIE_AGENT_SOCKET`; D23 (no token → NULL); D17 startup hooks read
  `host_config` in-process; Linux runtime risk added; 401 after startup handled by D24;
  Non-functional subsection added. Fixed all ℹ️: `test_plugin_sdk` credential test added
  to M4; one `fake_agent_socket` fixture; scanner tested on fixture strings; D25 settings
  route; guard message contents; D15 cites `kiro/mcp_server.py` and the reason for the
  override (uvicorn's `capture_signals` swaps SIGTERM handlers); D26 lazy `current()`;
  D27 port source; startup order and `os.environ` export; plugin READMEs in M5; the
  `wait_for_ready` hint. Second pass: one ❌ (exception types for a missing config or a
  failed miss) fixed with D28; all ⚠️ fixed (skills tests added to M1; the flag is set
  only around `source_from_env()` and guard calls because the in-process answer paths read
  the files; M2 owns the credential half of `sync()`; D29 splits failure handling between
  `sync()` and its callers); all ℹ️ fixed (`config_error` clearing, socket exposure stated
  in Non-functional, M5 dedupe, `migrate == 4` updates, the second `test_plugin_sdk` test,
  `run_container` env merge, `field(repr=False)`, module-attribute call and
  `real_source_from_env`). Stopped after two passes, as the process allows.

- Code review: `general` and `risk`, one round (no ❌). Fixed:
  - risk ⚠️: a failed first projected-env compute stayed cached for the whole session;
    `_runtime_env` now retries a failed compute after 30 s (`RETRY_AFTER`, patchable
    `_clock`) and keys the cache from before the compute (general ℹ️, the race).
    Test: `test_a_failed_first_compute_is_retried`.
  - risk ⚠️: sessions running at upgrade time break until restarted (mounted scripts and
    runner code). D21 corrected; upgrade step added to `docs/architecture.md` and Risks.
  - risk ℹ️: the nested uvicorn server reconfigured the process's uvicorn loggers; it now
    passes `log_config=None`, no `log_level`, `access_log=False`.
  - general ⚠️: the `wait_for_ready` re-run hint couldn't work (a failed session's token
    is rejected); replaced with a message saying so.
  - general ⚠️: the orchestrator-URL test was tautological; it now asserts a literal 7600
    and a configured 7611.
  - general ℹ️: typed-credential conversion was in three places and `local_api` had
    drifted; `credential_type_for()` / `to_credential()` in `credentials/models.py` are used
    by all three.
  - general ℹ️: the startup-hook test patched the module function; it installs a stand-in at
    `host_config.install()`. The write-and-sync helper is one function in
    `tests/host_config_helpers.py`. One test renamed to match what it checks. The plan's
    `guard_host_files` signature corrected.
  - general ❓ (local API failure on Kiro): now handled like the other startup failures
    (`_startup_failed`, startup-error file); test `test_kiro_lifespan_records_a_local_api_failure`.
  - risk ❓ (URL port from the edited config vs the bound port): answered, kept. The CLI
    already reaches the orchestrator through the same config port, so an edit without a
    restart breaks both alike; recorded under Risks.
  - Rejected: none.
- Merge risk: High: every new session now starts only if its container reaches the orchestrator at `host.docker.internal` with a valid token, which the tests cover in-process but no real container has exercised; a failure is loud (the agent exits 1 naming the cause) and reverting the PR restores the file reads.

## Outcome

Agent containers no longer read `config.yaml` or `credentials.yaml`. The agent syncs them
from `POST /api/agent/sync` (per-session token, hash stored in `sessions`), keeps a
pull-through cache with a 5-minute sync and an on-read OAuth sync, and serves its
subprocesses (exec runner plugins, `gh`, AWS `credential-process`) over a Unix socket. With
`ARCHIE_AGENT_CONTAINER=1` the shared file readers raise, and a static test keeps agent and
plugin code off them.

Verified: full suite `env -u ARCHIE_BACKEND scripts/dev test -q -p no:cacheprovider` 5618
passed; `scripts/dev ruff check .` clean; `ruff format --check` on the changed files clean;
shellcheck clean on `container/bin/gh`, `plugins/aws/bin/credential-process`, `scripts/dev`;
the `rg` ACs for the prompt and docs print nothing.

Not verified: a real session container end to end (Docker isn't available inside this
session), and reachability on Docker Desktop or native Linux (only colima was probed, and
only with `curl`).

PR: https://github.com/simon-downes/archie-nexus/pull/37
CI: none (no checks configured on the repository)

## Follow-ups

- Configurable host-side bind so agents on native Linux Docker can reach the orchestrator.
