# 022 — SPEC: Attach/exec routing via orchestrator proxy

## Objective

Connect clients to already-running sessions via the orchestrator's reverse proxy,
removing the last direct Docker calls from `cli.py` (except `shell`'s local exec).
`attach` and `start` route the TUI through the orchestrator. `shell` resolves the
session via the orchestrator, then execs directly as a host-local debug tool. The TUI
gains basic auto-reattach capability (D9).

## Context

This is M2b of project plan 019 (Orchestrator Control Plane). After M2a (021-spec),
the orchestrator owns session start/stop. This slice adds the proxy/connect path —
the orchestrator becomes the single ingress for all client↔session traffic (D8).
After this, cli.py is a pure protocol client.

Locked decisions inherited: D1 (control plane + ingress), D4 (decoupled lifecycle),
D8 (single client ingress, reverse proxy), D9 (auto-reattach).

Depends on: 020-spec-serve-and-ls (M1), 021-spec-session-lifecycle (M2a).

**Note on `SessionDescriptor.host`:** Project 019 originally called for adding a `host`
field in M2b. This was dropped during planning — the client already knows the
orchestrator address (it just connected), so a `host` field in the descriptor is
redundant. The orchestrator resolves the session's loopback target internally from
`SessionDescriptor.port`. Update project 019's "Cheap-now forward-compat" section to
reflect this decision.

## Requirements

- MUST add reverse-proxy routes to the orchestrator for session HTTP endpoints
  - AC: `GET /sessions/{id}/status` forwards to the session's `/status` and returns
    the response
  - AC: `GET /sessions/{id}/history` forwards to the session's `/history` and returns
    the response
  - AC: `POST /sessions/{id}/shell` forwards to the session's `POST /shell` and
    returns the response
  - AC: Returns 404 if session ID not found

- MUST add a WebSocket reverse-proxy route for the session stream
  - AC: `WS /sessions/{id}/stream` relays frames bidirectionally between client and
    session's `/stream`
  - AC: Client disconnect closes the backend connection
  - AC: Backend disconnect closes the client connection
  - AC: Session not found → WS close code 4004 with reason
  - AC: Backend unreachable → WS close code 4002 with reason

- MUST convert CLI `archie attach` to connect via the orchestrator proxy
  - AC: TUI connects to `ws://{orchestrator}/sessions/{id}/stream`
  - AC: TUI no longer connects directly to session container port
  - AC: Session resolution uses `GET /sessions` + client-side prefix matching

- MUST update CLI `archie start` (non-detach) to connect TUI via the orchestrator proxy
  - AC: After `POST /sessions` returns, TUI connects to
    `ws://{orchestrator}/sessions/{id}/stream`
  - AC: Same proxy path as `attach`

- MUST convert CLI `archie shell` to resolve the session via the orchestrator, then
  exec directly
  - AC: Session resolved via `GET /sessions` (not local Docker)
  - AC: `docker exec -it -w /workspace {container_name} bash` runs directly from CLI
  - AC: Docker remains a CLI dependency for `shell` only
  - Why: Shell is an interactive PTY, inherently host-local; proxying adds complexity
    for a debug tool that remote clients won't use

- MUST implement basic client auto-reattach (D9) in the TUI WebSocket client
  - AC: On WebSocket disconnect (close code 1006, 4002, or unexpected), TUI retries
    connecting to the orchestrator with exponential backoff
  - AC: On close code 4004 (session not found), TUI gives up immediately with error
  - AC: On successful reconnect, TUI fetches `/sessions/{id}/history` to catch up
    missed turns (deduplicating turns already displayed)
  - AC: TUI displays a "Reconnecting..." indicator during retry
  - AC: TUI gives up after 30s total with a clear error

- MUST remove all remaining Docker helpers from cli.py
  - AC: `list_sessions()`, `_query_port()`, `check_docker()`, `check_image()`,
    `_container_running()`, `_status_ok()`, `wait_for_ready()` removed from cli.py
  - AC: `CONTAINER_PORT`, `REPO_ROOT` constants removed
  - AC: Only `subprocess` import remaining is for `shell`'s `docker exec`
  - Why: After M2b, cli.py is a pure protocol client (D1/D8 fully locked)

- MUST handle the TUI's `!` prefix shell feature (`_run_direct_shell`) as a host-local
  exception
  - AC: TUI receives `container_name` at construction time (from session descriptor)
  - AC: `_run_direct_shell` uses the passed container_name for `docker exec`
  - AC: `POST /shell` (the logging endpoint) routes through the orchestrator proxy
  - Why: Same rationale as `archie shell` — interactive PTY, host-local debug tool

- SHOULD handle proxy errors gracefully
  - AC: Session container unreachable returns 502 to client
  - AC: Session container timeout returns 504 to client
  - AC: Error message distinguishes "session not found" (404) from "session
    unreachable" (502)

## Technical Design

### Proxy Routes (orchestrator app.py)

```python
Route("/sessions/{session_id}/status", endpoint=proxy_status, methods=["GET"]),
Route("/sessions/{session_id}/history", endpoint=proxy_history, methods=["GET"]),
Route("/sessions/{session_id}/shell", endpoint=proxy_shell, methods=["POST"]),
WebSocketRoute("/sessions/{session_id}/stream", endpoint=proxy_stream),
```

### New File: `orchestrator/src/archie_orchestrator/proxy.py`

**Session resolution:**
```python
def _resolve_session(session_id: str) -> SessionDescriptor:
    """Find session by exact ID. Raises KeyError (404) or ValueError (503)."""
    sessions = list_sessions()
    for s in sessions:
        if s.session_id == session_id:
            if s.port is None:
                raise ValueError(f"Session '{session_id}' has no port (still starting?)")
            return s
    raise KeyError(f"No session with ID '{session_id}'")
```

**HTTP forwarding (for /status, /history, /shell):**
```python
async def forward_http(session: SessionDescriptor, path: str,
                       method: str = "GET", body: bytes = b"") -> Response:
    target = f"http://127.0.0.1:{session.port}{path}"
    async with httpx.AsyncClient() as client:
        if method == "GET":
            resp = await client.get(target, timeout=5.0)
        else:
            resp = await client.post(target, content=body, timeout=5.0)
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))
```

**WebSocket relay:**
```python
async def proxy_stream(websocket: WebSocket):
    session_id = websocket.path_params["session_id"]
    try:
        session = _resolve_session(session_id)
    except KeyError:
        await websocket.close(code=4004, reason="Session not found")
        return

    target_url = f"ws://127.0.0.1:{session.port}/stream"
    await websocket.accept()

    try:
        async with websockets.connect(target_url) as backend:
            async def client_to_backend():
                try:
                    async for msg in websocket.iter_text():
                        await backend.send(msg)
                except WebSocketDisconnect:
                    pass

            async def backend_to_client():
                async for msg in backend:
                    if isinstance(msg, str):
                        await websocket.send_text(msg)
                    else:
                        await websocket.send_bytes(msg)

            done, pending = await asyncio.wait(
                [asyncio.create_task(client_to_backend()),
                 asyncio.create_task(backend_to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except (ConnectionRefusedError, OSError):
        await websocket.close(code=4002, reason="Session unreachable")
```

Uses Starlette's `WebSocket` (server-side) + `websockets` library (client-side to
backend). Both are async; frame types pass through as-is (text remains text, binary
remains binary).

### Error Mapping

| Condition | HTTP routes | WebSocket route |
|-----------|-------------|-----------------|
| Session not found | 404 JSON | close 4004 |
| Session no port | 503 JSON | close 4003 |
| Backend unreachable | 502 JSON | close 4002 |
| Backend timeout | 504 JSON | (relay handles naturally) |

### Orchestrator Dependencies Update

```toml
# orchestrator/pyproject.toml
dependencies = [
    "archie-shared",
    "httpx>=0.27",
    "starlette>=0.40",
    "uvicorn>=0.30",
    "websockets>=13",
]
```

### TUI Refactor

**Constructor change:** `ArchieApp(host, port)` → `ArchieApp(ws_url, api_url, container_name)`

- `ws_url`: `ws://127.0.0.1:7600/sessions/{id}/stream`
- `api_url`: `http://127.0.0.1:7600/sessions/{id}`
- `container_name`: from `SessionDescriptor.container_name` (for `!` shell feature)

**Endpoint usage:**
- WebSocket: connect to `ws_url` (was `ws://{host}:{port}/stream`)
- Status: `GET {api_url}/status` (was `http://{host}:{port}/status`)
- History: `GET {api_url}/history` (was `http://{host}:{port}/history`)
- Shell log: `POST {api_url}/shell` (was `http://{host}:{port}/shell`)
- Direct shell (`!` prefix): `docker exec` using `container_name` — host-local, unchanged

### Auto-Reattach (adapting existing `_reconnect()` logic)

The TUI already has `_reconnect()` (tui/app.py ~line 164) with backoff delays
`[1.0, 2.0, 4.0, 8.0, 8.0]` and history catch-up. This is adapted, not rewritten:

- Change the reconnect target from direct session URL to orchestrator proxy URL
- Add close-code awareness:
  - 4004 (session not found) → give up immediately, show "session ended" message
  - 4002 (unreachable) / 1006 (abnormal) / unexpected → retry with backoff
- Add deduplication: on history catch-up, compare turn indices with already-displayed
  turns, only render new ones
- Keep the existing "Reconnecting..." indicator pattern
- Adjust total timeout to 30s

### CLI Changes

**Shared helper:**
```python
def _session_urls(session_id: str) -> tuple[str, str]:
    """Return (ws_url, api_url) for a session via orchestrator."""
    base = _orchestrator_url()
    ws_base = base.replace("http://", "ws://")
    return (
        f"{ws_base}/sessions/{session_id}/stream",
        f"{base}/sessions/{session_id}",
    )
```

**`attach`:**
```python
def attach(session_id: str | None):
    url = _orchestrator_url()
    sessions = _fetch_sessions(url)  # GET /sessions, decode
    target = _resolve_prefix(sessions, session_id)
    ws_url, api_url = _session_urls(target.session_id)
    app = ArchieApp(ws_url=ws_url, api_url=api_url,
                    container_name=target.container_name)
    app.run()
```

**`start` (non-detach):**
```python
    descriptor = _parse_response(response)  # SessionDescriptor from POST /sessions
    ws_url, api_url = _session_urls(descriptor.session_id)
    app = ArchieApp(ws_url=ws_url, api_url=api_url,
                    container_name=descriptor.container_name)
    app.run()
```

**`shell`:**
```python
def shell(session_id: str | None):
    url = _orchestrator_url()
    sessions = _fetch_sessions(url)
    target = _resolve_prefix(sessions, session_id)
    exec_cmd = ["docker", "exec", "-it", "-w", "/workspace",
                target.container_name, "bash"]
    result = subprocess.run(exec_cmd, check=False)
    sys.exit(result.returncode)
```

### cli.py Cleanup

Remove after all commands are migrated:
- `list_sessions()`, `_query_port()`, `check_docker()`, `check_image()`
- `_container_running()`, `_status_ok()`, `wait_for_ready()`
- `CONTAINER_PORT`, `REPO_ROOT` constants
- `import json`, `import time`, `import urllib.request`, `import urllib.error`
- Keep: `import subprocess` (for shell), `_pick_session()` (client-side UI)

## Milestones

### 1. Implement HTTP proxy routes (`/sessions/{id}/status`, `/history`, `/shell`)

**Approach:**
- Create `orchestrator/src/archie_orchestrator/proxy.py` with `_resolve_session()` and
  `forward_http()`.
- Add `httpx>=0.27` to orchestrator dependencies.
- Add three routes to `app.py`: GET status, GET history, POST shell.
- Each resolves the session by exact ID (via `list_sessions()`), forwards to
  `http://127.0.0.1:{port}/{path}`, returns the response.
- POST /shell forwards the request body as-is to the session's POST /shell.
- Error handling: 404 (not found), 502 (unreachable), 503 (no port), 504 (timeout).
- Test seam: mock `list_sessions()` and `httpx.AsyncClient`.

**Edge cases:**
- Session not found → 404 with JSON error body
- Session has no port (still starting) → 503
- Backend container crashed/unreachable → 502 (httpx.ConnectError)
- Backend timeout → 504 (httpx.TimeoutException)

**Tasks:**
- Create `orchestrator/src/archie_orchestrator/proxy.py`
- Implement `_resolve_session()` helper
- Implement `forward_http()` async function (supports GET and POST)
- Add `proxy_status`, `proxy_history`, `proxy_shell` route handlers to `app.py`
- Add `httpx>=0.27` to `orchestrator/pyproject.toml`
- Create `tests/test_orchestrator_proxy.py`
- Test: GET `/sessions/{id}/status` → forwarded 200 response
- Test: GET `/sessions/{id}/history` → forwarded response
- Test: POST `/sessions/{id}/shell` → forwarded with body
- Test: unknown session → 404
- Test: backend unreachable → 502
- Test: backend timeout → 504

**Deliverable:** HTTP proxy routes forward status, history, and shell requests to
session containers.

**Verify:** `pytest tests/test_orchestrator_proxy.py -v` — all pass.

---

### 2. Implement WebSocket proxy route (`/sessions/{id}/stream`)

**Approach:**
- Add `websockets>=13` to orchestrator dependencies.
- Add WebSocketRoute to `app.py`.
- Implementation uses Starlette `WebSocket` (server, accepts client) +
  `websockets.connect()` (client, connects to backend). These are different APIs in
  the same process — Starlette for inbound, websockets library for outbound.
- On connect: resolve session, open backend WS to `ws://127.0.0.1:{port}/stream`.
- Relay frames bidirectionally using two async tasks (`client→backend`,
  `backend→client`). Use `asyncio.wait(FIRST_COMPLETED)` and cancel the other.
- Handle both text and binary frames (pass through as-is).
- Error handling: session not found → accept then close with 4004; backend refuses →
  close with 4002.
- Test seam: mock `websockets.connect` to a fake backend; use Starlette TestClient WS.
- ⚠️ Ensure clean shutdown: when either side disconnects, the other is closed promptly
  (no leaked tasks/connections).

**Edge cases:**
- Session not found → close 4004 with reason "Session not found"
- Session no port → close 4003 with reason "Session not ready"
- Backend refuses connection → close 4002 with reason "Session unreachable"
- Client disconnects mid-stream → backend connection closed, task cancelled
- Backend disconnects mid-stream → client notified via close frame or error

**Tasks:**
- Add `websockets>=13` to `orchestrator/pyproject.toml`
- Implement `proxy_stream` WebSocket handler in `proxy.py`
- Register `WebSocketRoute` in `app.py`
- Create `tests/test_orchestrator_ws_proxy.py`
- Test: successful bidirectional text relay (mock backend)
- Test: session not found → close 4004
- Test: backend unreachable → close 4002
- Test: client disconnect → backend closed
- Test: backend disconnect → client closed

**Deliverable:** WebSocket proxy relays the agent stream bidirectionally through the
orchestrator.

**Verify:** `pytest tests/test_orchestrator_ws_proxy.py -v` — all pass.

---

### 3. Refactor TUI to use orchestrator URLs and adapt auto-reattach

**Approach:**
- Change `ArchieApp.__init__` from `(host, port)` to `(ws_url, api_url, container_name)`.
  - `ws_url`: orchestrator's WS proxy endpoint
  - `api_url`: orchestrator's HTTP proxy base for this session
  - `container_name`: for the `!` shell feature (host-local exec)
- Update `ws_client.py` to connect to the provided `ws_url`.
- Update all HTTP fetches in the TUI (status, history, shell log) to use `api_url`.
- **Adapt existing `_reconnect()` logic** (tui/app.py ~line 164) — this is a refactor
  of existing code, not greenfield:
  - Change reconnect target from direct session URL to `ws_url` (orchestrator proxy)
  - Add close-code awareness: 4004 → give up immediately; 4002/1006 → retry
  - Add turn deduplication on history catch-up: compare `turn_index` with
    highest already-displayed turn, only render new turns
  - Adjust total timeout to 30s
  - Keep existing "Reconnecting..." indicator
- The `!` prefix shell (`_run_direct_shell`) uses `self._container_name` for
  `docker exec` — unchanged behaviour, just wired to the passed-in value instead of
  deriving from session_id.
- Test seam: mock WebSocket connections; test reconnect flow.
- ⚠️ History deduplication is the one genuinely new piece — existing `_reconnect()`
  replays all history which causes duplicate display if the disconnect was brief.

**Tasks:**
- Refactor `ArchieApp.__init__` signature and internal state
- Update `ws_client.py`: connect to `ws_url`
- Update status fetch: `GET {api_url}/status`
- Update history fetch: `GET {api_url}/history`
- Update shell log: `POST {api_url}/shell`
- Update `_run_direct_shell`: use `self._container_name`
- Adapt `_reconnect()`:
  - Target: `ws_url`
  - Close-code handling (4004 → give up, 4002/1006 → retry)
  - History dedup on catch-up
  - 30s total timeout
- Update `tests/test_ws_client.py`:
  - Test: connect to provided ws_url
  - Test: disconnect → reconnect → history catch-up (deduped)
  - Test: 4004 close → immediate give-up
  - Test: 30s timeout → error shown
- Update `tests/test_tui_input.py` if constructor change breaks fixtures

**Deliverable:** TUI connects via orchestrator proxy URLs and auto-reattaches on
disconnect with deduplicated history catch-up.

**Verify:** `pytest tests/test_ws_client.py -v && pytest tests/test_tui_input.py -v`
— all pass.

---

### 4. Convert CLI `attach` and `start` (non-detach) to use orchestrator proxy

**Approach:**
- Add shared helpers to cli.py:
  - `_fetch_sessions(url) -> list[SessionDescriptor]` — GET /sessions, decode
  - `_resolve_prefix(sessions, prefix) -> SessionDescriptor` — client-side prefix
    match + picker (uses existing `_pick_session()`)
  - `_session_urls(session_id) -> tuple[str, str]` — constructs (ws_url, api_url)
    from orchestrator config
- `attach`: fetch sessions → resolve prefix → construct URLs → ArchieApp(ws_url,
  api_url, container_name)
- `start` (non-detach): after POST /sessions, construct URLs from returned descriptor
  → ArchieApp(ws_url, api_url, container_name)
- Remove `check_docker()` from `attach`.
- Error handling: orchestrator unreachable → "archie serve" message.
- URLs are constructed from orchestrator config (not from session port — that's internal).

**Tasks:**
- Add `_fetch_sessions()` helper
- Add `_resolve_prefix()` helper (extracted from duplicated logic in attach/shell/stop)
- Add `_session_urls()` helper
- Rewrite `attach`: use helpers → ArchieApp with orchestrator URLs
- Update `start` non-detach: construct URLs from response → ArchieApp
- Remove `check_docker()` from `attach`
- Error handling for orchestrator unreachable
- Create `tests/test_cli_attach.py`
- Test: `attach` resolves and constructs correct URLs
- Test: `attach` with prefix matching
- Test: `start` non-detach launches TUI with correct URLs
- Test: orchestrator unreachable → error message

**Deliverable:** `attach` and `start` connect the TUI through the orchestrator proxy.

**Verify:** `pytest tests/test_cli_attach.py -v` — all pass.

---

### 5. Convert CLI `shell` and clean up cli.py

**Approach:**
- `shell`: use `_fetch_sessions()` + `_resolve_prefix()` to get the target session,
  then run `docker exec -it -w /workspace {container_name} bash` directly.
- Remove all remaining Docker helpers from cli.py:
  - `list_sessions()`, `_query_port()`, `check_docker()`, `check_image()`
  - `_container_running()`, `_status_ok()`, `wait_for_ready()`
  - `CONTAINER_PORT`, `REPO_ROOT` constants
  - Unused imports: `json`, `time`, `urllib.request`, `urllib.error`, `os` (if unused)
- Keep: `import subprocess` (for shell), `_pick_session()` (used by `_resolve_prefix`),
  `import sys` (for exit code).
- Verify no other code in the cli package references removed functions.
- ⚠️ The TUI's `_run_direct_shell` still uses `subprocess` + `docker exec` — this is
  intentional and documented as the host-local exception.

**Tasks:**
- Rewrite `shell`: `_fetch_sessions()` → `_resolve_prefix()` → docker exec
- Remove Docker helpers from cli.py (listed above)
- Remove unused imports and constants
- Verify: grep cli/ for any remaining references to removed functions
- Create `tests/test_cli_shell.py`
- Test: shell resolves session via orchestrator and constructs correct exec command
- Test: shell with no sessions → error
- Test: shell with prefix matching
- Run full suite to verify no regressions

**Deliverable:** `shell` works via orchestrator resolution + direct exec; cli.py has
no Docker helpers remaining; the CLI package is a pure protocol client (except the
documented host-local shell exception).

**Verify:**
1. `pytest tests/test_cli_shell.py -v` — all pass
2. `pytest tests/ -v` — full suite, no regressions
3. `ruff check cli/` — no unused imports, no dead code
4. Manual e2e deferred to user's host environment

---

## Not yet specified

- Auto-reattach hardening under orchestrator restart (distinguishing "orchestrator
  bounced" from "session died") — M3's scope
- Binary frame handling (current protocol is text/JSON only; relay passes through but
  no binary is sent today)
- Proxy authentication — deferred (see 019 D10: client↔orchestrator auth is a
  later additive slice; the ingress proxy is unauthenticated on a trusted network)
- Remote `shell` equivalent (WebSocket PTY proxy) — future, if ever needed
