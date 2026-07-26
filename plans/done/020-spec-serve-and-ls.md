# 020 — SPEC: `archie serve` + `ls` tracer bullet

## Objective

Stand up the orchestrator as a new `orchestrator/` workspace member with a long-running
`archie serve` command, expose `GET /sessions` (migrating the `docker ps` parsing from
cli.py), and convert `archie ls` to call the orchestrator API. This is the
architecture-locking tracer bullet — proving the client→orchestrator→Docker path
end-to-end with the simplest possible operation.

## Context

This is M1 of project plan 019 (Orchestrator Control Plane). Today `cli.py` shells
directly to Docker for everything. This slice introduces the orchestrator process and
moves exactly one operation through it. No functional change to end-user behaviour —
`archie ls` returns the same data, just via the orchestrator.

Locked decisions inherited from 019: D1 (control plane only), D4 (decoupled lifecycle),
D6 (`archie serve`), D7 (backend addr is per-model catalog data; `--add-host` stays),
D8 (single client ingress, sessions loopback-only), D9 (clients auto-reattach).

## Requirements

- MUST add a new `orchestrator/` workspace member (package `archie-orchestrator`) to the
  uv workspace, depending on `archie-shared`, `starlette`, and `uvicorn`
  - AC: `orchestrator/` appears in root `pyproject.toml` workspace members
  - AC: `uv sync` succeeds with the new member
  - AC: `from archie_orchestrator import ...` works from the dev venv

- MUST expose `GET /sessions` returning current running sessions as JSON-serialized
  `list[SessionDescriptor]`
  - AC: Response is a JSON array of objects matching `SessionDescriptor` fields
  - AC: Each session includes `session_id`, `container_name`, `port` (nullable),
    `raw_docker_status`

- MUST discover sessions by parsing `docker ps` output and querying ports via
  `docker port` (the logic currently in `cli.py:list_sessions()`)
  - AC: Running archie containers are detected by name pattern (`CONTAINER_PREFIX`)
  - AC: Port mapping is resolved per container
  - AC: Non-archie containers are excluded

- MUST start as a foreground process via `archie serve`, bound to `127.0.0.1:7600` by
  default
  - AC: `archie serve` starts a long-running process that responds to HTTP requests on
    port 7600
  - AC: Process stays in foreground (no daemonization)
  - AC: Ctrl+C stops the process cleanly

- MUST allow bind address/port override via `~/.nexus/config.yaml` under an
  `orchestrator` key
  - AC: Setting `orchestrator.port: 8800` causes the server to bind to port 8800
  - AC: Unset config uses the default (7600)

- MUST log the bound address on startup
  - AC: A line containing the host:port is emitted to stdout or stderr before accepting
    connections

- MUST convert `archie ls` to call the orchestrator's `GET /sessions` endpoint
  - AC: `archie ls` makes an HTTP request to the orchestrator and displays the result
  - AC: Output format is unchanged from current behaviour

- MUST fail with a clear error and non-zero exit code when the orchestrator is
  unreachable
  - AC: Connection refused produces a message containing "archie serve" as the remedy
  - AC: Exit code is non-zero

- MUST NOT fall back to direct Docker commands when the orchestrator is unreachable
  - AC: No `docker ps` subprocess is spawned by `archie ls`
  - Why: The tracer bullet must prove the orchestrator path; silent fallback masks
    failures

- SHOULD expose `GET /health` returning 200 OK for liveness probes
  - AC: `GET /health` returns HTTP 200 with a JSON body

- MUST leave the existing `list_sessions()` in `cli.py` functional for other commands
  (stop/attach/shell still use it directly until M2a/M2b)
  - AC: `archie stop`, `archie attach`, `archie shell` continue to work unchanged
  - Why: Only `ls` migrates in this slice; other commands migrate in M2a/M2b

## Technical Design

### Code Location & Package Structure

New workspace member following the existing pattern:

```
orchestrator/
├── pyproject.toml
└── src/
    └── archie_orchestrator/
        ├── __init__.py
        ├── app.py          # Starlette routes
        └── docker.py       # docker ps/port subprocess logic
```

The `archie serve` command lives in `cli/` (it's a CLI subcommand) but imports and runs
the orchestrator's app. This matches the existing pattern: `archie-agent` has its own
entrypoint for the container, while the host CLI is the user-facing surface.

### Dependencies

```toml
# orchestrator/pyproject.toml
[project]
name = "archie-orchestrator"
version = "0.1.0"
description = "Archie orchestrator — host-side control plane"
requires-python = ">=3.13,<3.14"
dependencies = [
    "archie-shared",
    "starlette>=0.40",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/archie_orchestrator"]
```

The `cli` package gains a dependency on `archie-orchestrator` (for `archie serve` to
import and run the app).

### Config Schema

Extend `NexusConfig` in `shared/src/archie_shared/schemas.py`. Follow the existing
pattern: all section structs use `forbid_unknown_fields=True` and are wired into
`NexusConfig` via `msgspec.field(default_factory=...)`.

```python
class OrchestratorConfig(msgspec.Struct, forbid_unknown_fields=True):
    host: str = "127.0.0.1"
    port: int = 7600

class NexusConfig(msgspec.Struct, forbid_unknown_fields=True):
    ...
    orchestrator: OrchestratorConfig = msgspec.field(default_factory=OrchestratorConfig)
```

Both `archie serve` (bind address) and `archie ls` (connect address) read this config
via `load_nexus_config()`.

### API Contract

`GET /sessions` returns:
```json
[
  {
    "session_id": "myproject-01jfoo",
    "container_name": "archie-myproject-01jfoo",
    "port": 32771,
    "raw_docker_status": "Up 2 hours"
  }
]
```

Serialized via `msgspec.json.encode()` → `starlette.responses.Response(content,
media_type="application/json")`. Standard `JSONResponse` does NOT work with msgspec
Structs — use msgspec encoding directly.

`GET /health` returns:
```json
{"status": "ok"}
```

### CLI Integration

**`archie serve`** in `cli/src/archie_cli/cli.py`:
- Reads `OrchestratorConfig` from `load_nexus_config()`
- Calls `uvicorn.run("archie_orchestrator.app:app", host=cfg.orchestrator.host,
  port=cfg.orchestrator.port)`

**`archie ls`** modified path:
- Calls `load_nexus_config()` to get orchestrator host/port
- Uses `httpx.get(f"http://{host}:{port}/sessions")` (httpx already a cli dep)
- Deserializes via `msgspec.json.decode(response.content, type=list[SessionDescriptor])`
- Formats table (same output as today)
- On `httpx.ConnectError`: raises `click.ClickException` with message including
  "archie serve" as the remedy

A small helper (e.g. `_orchestrator_url() -> str`) keeps the config-read + URL
construction DRY for future commands that will also call the orchestrator.

### Docker Logic Migration

`orchestrator/src/archie_orchestrator/docker.py` contains
`list_sessions() -> list[SessionDescriptor]` — the same logic as today's
`cli.py:list_sessions()` (line 121). This is a **move**, not a refactor. The function
in cli.py stays (requirement 11) until M2a/M2b remove it.

## Milestones

### 1. Scaffold the `orchestrator/` workspace member

**Approach:**
- Follow the exact pattern of `agent/` and `cli/`: `pyproject.toml` with hatchling
  build, `src/` layout.
- Add `"orchestrator"` to root `pyproject.toml` workspace members and dev deps.
- Add `archie-orchestrator = { workspace = true }` to `[tool.uv.sources]`.
- Add `"archie-orchestrator"` to `cli/pyproject.toml` dependencies.

**Tasks:**
- Create `orchestrator/pyproject.toml`
- Create `orchestrator/src/archie_orchestrator/__init__.py`
- Update root `pyproject.toml`: workspace members, dev deps, uv.sources
- Update `cli/pyproject.toml`: add `archie-orchestrator` dependency
- Run `uv sync`

**Deliverable:** Package resolves in dev venv.

**Verify:** `uv sync && python -c "import archie_orchestrator"`

---

### 2. Implement Docker session discovery in the orchestrator

**Approach:**
- Create `orchestrator/src/archie_orchestrator/docker.py` with `list_sessions()`.
- Port the logic from `cli.py:121–153` (`docker ps` → parse JSON → `parse_container_name`
  → `_query_port` → build `SessionDescriptor`).
- Import `SessionDescriptor`, `parse_container_name`, `CONTAINER_PREFIX` from
  `archie_shared.session`.
- Keep `_query_port` as a private helper in the same module.
- Test seam: `list_sessions()` depends on `subprocess.run` — mock it in tests.

**Tasks:**
- Create `orchestrator/src/archie_orchestrator/docker.py`
- Implement `list_sessions() -> list[SessionDescriptor]`
- Implement `_query_port(name: str) -> str | None`
- Create `tests/test_orchestrator_docker.py`
- Test: mocked `docker ps` with two archie containers → correct descriptors
- Test: empty `docker ps` → returns `[]`
- Test: non-archie containers in output → filtered out

**Deliverable:** `archie_orchestrator.docker.list_sessions()` returns correct session
data from Docker subprocess output.

**Verify:** `pytest tests/test_orchestrator_docker.py -v` — all pass.

---

### 3. Implement the orchestrator Starlette app with `GET /sessions` and `GET /health`

**Approach:**
- Create `orchestrator/src/archie_orchestrator/app.py` with a Starlette app.
- `GET /health` → `Response(msgspec.json.encode({"status": "ok"}),
  media_type="application/json")` (or `JSONResponse` is fine here since it's a plain
  dict).
- `GET /sessions` → call `list_sessions()` from `docker.py`, serialize result via
  `msgspec.json.encode(sessions)`, return as
  `Response(content, media_type="application/json")`.
- No lifespan needed yet (no state to initialize).
- Test seam: HTTPX `TestClient` against the Starlette app with `list_sessions` patched.

**Tasks:**
- Create `orchestrator/src/archie_orchestrator/app.py`
- Implement `GET /health` route
- Implement `GET /sessions` route using msgspec encoding
- Create `tests/test_orchestrator_app.py`
- Test: `GET /health` returns 200 + `{"status": "ok"}`
- Test: `GET /sessions` with patched docker returns serialized SessionDescriptors
- Test: `GET /sessions` with no containers returns `[]`

**Deliverable:** The orchestrator app responds correctly to HTTP requests.

**Verify:** `pytest tests/test_orchestrator_app.py -v` — all pass.

---

### 4. Add `OrchestratorConfig` to the shared config schema

**Approach:**
- Add `OrchestratorConfig` struct to `shared/src/archie_shared/schemas.py`, using
  `forbid_unknown_fields=True` (matches existing section pattern).
- Add `orchestrator: OrchestratorConfig = msgspec.field(default_factory=OrchestratorConfig)`
  to `NexusConfig`.
- ⚠️ Existing config files without an `orchestrator` key must still load — the
  `default_factory` ensures this. Verify in tests.

**Tasks:**
- Add `OrchestratorConfig` struct to `schemas.py`
- Add `orchestrator` field to `NexusConfig`
- Update `tests/test_nexus_config.py`:
  - Test: config without `orchestrator` key loads with defaults (host=127.0.0.1,
    port=7600)
  - Test: config with `orchestrator: {port: 8800}` loads correctly
  - Test: config with unknown key under `orchestrator` raises validation error

**Deliverable:** `load_nexus_config()` returns orchestrator host/port from config or
defaults.

**Verify:** `pytest tests/test_nexus_config.py -v` — existing + new tests pass.

---

### 5. Add `archie serve` command and convert `archie ls`

**Approach:**
- Add `serve` command to `cli.py`: load config via `load_nexus_config()`, call
  `uvicorn.run("archie_orchestrator.app:app", host=..., port=...)`.
- Add a helper `_orchestrator_url() -> str` that loads config and returns the base URL.
- Modify `ls_cmd`: call `_orchestrator_url()` → `httpx.get(url + "/sessions")` →
  `msgspec.json.decode(response.content, type=list[SessionDescriptor])` → format table.
- On `httpx.ConnectError`: `raise click.ClickException(...)` with message containing
  "archie serve".
- `ls_cmd` no longer calls `check_docker()` (that's the orchestrator's concern).
- ⚠️ Keep the existing `list_sessions()` and helpers in cli.py — other commands still
  use them.

**Tasks:**
- Add `archie serve` command to cli.py
- Add `_orchestrator_url()` helper to cli.py
- Modify `ls_cmd`: replace direct Docker call with HTTP path
- Add error handling for `httpx.ConnectError`
- Remove `check_docker()` call from `ls_cmd`
- Create `tests/test_orchestrator_cli.py`
- Test: `archie ls` with mocked HTTP response → correct table output
- Test: `archie ls` when orchestrator unreachable → error contains "archie serve",
  exit code non-zero
- Test: `archie serve` invokes uvicorn with correct host/port from config

**Deliverable:** `archie serve` starts the orchestrator; `archie ls` lists sessions via
the orchestrator API; unreachable orchestrator produces a clear error.

**Verify:**
1. `pytest tests/test_orchestrator_cli.py -v` — all pass
2. Manual end-to-end verification on the host (start serve, run ls, kill serve, run ls
   again) — deferred to user's environment

---

## Not yet specified

- Logging configuration (structured vs plain, log level) — defer until a real need
  surfaces (uvicorn defaults suffice for now)
- Signal handling (SIGTERM/SIGINT graceful shutdown) — likely handled by uvicorn
  defaults; verify in M3
