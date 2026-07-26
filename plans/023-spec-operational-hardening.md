# 023 — SPEC: Operational hardening (logging, errors, bounce survival)

## Objective

Add operational logging, error resilience, and prove the lifecycle-decoupling
invariant — restarting the orchestrator doesn't kill sessions, and clients
transparently reconnect. Harden auto-reattach (D9) from M2b's basic retry into a
proven, sub-second-invisible feature by tuning the reconnect timing.

## Context

This is M3 of project plan 019 (Orchestrator Control Plane). After M2b, the
orchestrator proxies all client↔session traffic (D8). An orchestrator bounce severs
every attached client. D9 and `/history` catch-up make this transparent. This slice
proves that by exercising it deliberately, and adds operational logging + error
resilience so the orchestrator is production-ready for daily use.

Locked decisions: D4 (decoupled lifecycle), D6 (archie serve), D8 (ingress proxy),
D9 (auto-reattach).

Depends on: 020, 021, 022 (M1, M2a, M2b) — **all must be landed before M3 begins.**
This plan adds logging and error handling to files created by those specs (lifecycle.py,
proxy.py, docker.py, app.py in the orchestrator package).

**Scope narrowing from 019's M3:** Project 019's M3 included `serve stop/restart/reload`.
These are deferred:
- `stop/restart`: Ctrl+C is sufficient for foreground use; systemd handles this later.
- `reload`: The orchestrator is effectively stateless (reads config per-request); there
  is nothing meaningful to hot-reload today.
- Clean session termination signal: `docker stop` + SIGTERM is graceful enough; JSONL
  is append-per-event so no data is lost.

## Requirements

- MUST add operational logging to the orchestrator using Python `logging` (plain text
  to stderr)
  - AC: Session start logged: "Session started: {id} (workspace: {name})"
  - AC: Session stop logged: "Session stopped: {id}"
  - AC: Client proxy connect logged: "Client connected: {session_id}"
  - AC: Client proxy disconnect logged: "Client disconnected: {session_id}"
  - AC: Startup logs discovered sessions: "Discovered N running sessions"
  - AC: Shutdown logged: "Orchestrator stopping ({N} active connections)"

- MUST handle all errors/exceptions so the orchestrator never crashes from an uncaught
  exception
  - AC: A global Starlette exception handler catches unexpected errors (via
    `app.add_exception_handler(Exception, handler)`)
  - AC: Unexpected errors return HTTP 500 with a helpful message (not a raw traceback)
  - AC: Unexpected errors are logged with origin file:line and a descriptive message
  - AC: The orchestrator process stays running after any single-request failure
  - AC: WebSocket proxy errors are caught within the handler (Starlette doesn't route
    WS errors through HTTP exception handlers)

- MUST handle expected errors per-route with helpful messages
  - AC: Docker subprocess failures produce helpful error via a `DockerError` exception
    (not raw `CalledProcessError` or `FileNotFoundError`)
  - AC: `DockerError` includes the failed command and stderr output
  - AC: Session-not-found, workspace-not-found, image-not-found all have clear messages
  - AC: WebSocket proxy errors (backend crash/refuse) log and close cleanly without
    affecting other proxy connections

- MUST survive orchestrator restart without killing sessions (D4)
  - AC: After Ctrl+C and re-running `archie serve`, all previously running sessions
    appear in `GET /sessions`
  - AC: Orchestrator shutdown path does NOT call `docker stop` on any container
  - AC: Sessions started with `--rm` continue running (Docker manages lifecycle
    independently of the orchestrator process)
  - Why: The orchestrator is stateless — it rediscovers sessions via docker ps on
    every request. No "rediscovery" logic is needed; it's the default behaviour.

- MUST prove client auto-reattach after orchestrator bounce (D9) via integration tests
  - AC: TUI reconnects automatically when orchestrator restarts
  - AC: History catch-up path is exercised (even if zero turns were missed — validates
    the reconnect flow end-to-end)
  - AC: No duplicate turns are rendered on reconnect
  - AC: Agent loop continues running during the disconnect (broadcast to empty clients
    set is a no-op)
  - Why: During a local bounce, the common case is zero missed turns (agent only
    produces output when a client sends a message). The test validates the mechanism;
    the "missed turns" scenario occurs when a second client sends a message during the
    disconnect window.

- MUST tune TUI reconnect timing for fast local recovery
  - AC: Initial retry delay is 0.5s (reduced from M2b's 1.0s)
  - AC: Local orchestrator bounce results in reconnect within 2 seconds (typical)
  - AC: Backoff schedule: 0.5s, 1s, 2s, 4s, 8s (cap), 30s total timeout
  - Why: D8 means every orchestrator restart bounces all clients. This must be
    invisible in practice, not a 3-5 second disruption.

- SHOULD log at appropriate levels
  - AC: INFO for lifecycle events (start, stop, connect, disconnect)
  - AC: WARNING for recoverable errors (backend unreachable, Docker command failed)
  - AC: ERROR for unexpected failures (unhandled exceptions)
  - AC: DEBUG for subprocess commands (docker ps, docker port, etc.)
  - AC: Log level configurable via `LOG_LEVEL` env var (default: INFO)

## Technical Design

### Logging Setup

Configure in orchestrator package (`orchestrator/src/archie_orchestrator/__init__.py`
or a dedicated module):

```python
import logging
import os

def configure_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

log = logging.getLogger("archie.orchestrator")
```

Called early in app lifespan (before uvicorn's own logging setup can interfere).

### Log Points

| Location | Level | Message |
|----------|-------|---------|
| `lifecycle.py` start_session | INFO | "Session started: {id} (workspace: {name})" |
| `lifecycle.py` stop_session | INFO | "Session stopped: {id}" |
| `proxy.py` proxy_stream accept | INFO | "Client connected: {session_id}" |
| `proxy.py` proxy_stream close | INFO | "Client disconnected: {session_id}" |
| `app.py` lifespan startup | INFO | "Discovered {N} running sessions" |
| `app.py` lifespan shutdown | INFO | "Orchestrator stopping ({N} active connections)" |
| `docker.py` subprocess calls | DEBUG | "docker {command}" |
| `proxy.py` backend unreachable | WARNING | "Backend unreachable for {session_id}: {error}" |
| `docker.py` command failure | WARNING | "Docker command failed: {cmd}: {stderr}" |
| Global handler | ERROR | "Unhandled error: {exc} (at {file}:{line})" |

### Global Exception Handler

```python
import traceback
from starlette.requests import Request
from starlette.responses import JSONResponse

async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    tb = traceback.extract_tb(exc.__traceback__)
    origin = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "unknown"
    log.error(f"Unhandled error: {exc} (at {origin})")
    return JSONResponse(
        {"error": "Internal server error", "detail": str(exc)},
        status_code=500,
    )

# Register:
app.add_exception_handler(Exception, unhandled_exception_handler)
```

For WebSocket routes (not covered by HTTP exception handlers):
```python
async def proxy_stream(websocket: WebSocket):
    try:
        # ... existing relay logic ...
    except Exception as exc:
        tb = traceback.extract_tb(exc.__traceback__)
        origin = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "unknown"
        log.error(f"WS proxy error for {session_id}: {exc} (at {origin})")
        try:
            await websocket.close(code=1011, reason="Internal error")
        except Exception:
            pass  # Client may already be gone
```

### DockerError

```python
class DockerError(Exception):
    """Raised when a Docker CLI command fails."""

    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"Docker command failed ({command}): {stderr.strip()}")
```

All `subprocess.run` calls in `docker.py` that check returncode wrap failures:
```python
result = subprocess.run(cmd, capture_output=True, text=True, check=False)
if result.returncode != 0:
    raise DockerError(
        command=cmd[0:3],  # e.g. ["docker", "ps", "--filter"]
        stderr=result.stderr,
        returncode=result.returncode,
    )
```

Route handlers catch `DockerError` → return 500 with the message (or 503 for
"daemon unavailable" pattern).

### Reconnect Timing Adjustment

In TUI's reconnect logic (M2b's `_reconnect` / `connect_with_retry`):
```python
BACKOFF_DELAYS = [0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]  # 30s total cap
```

Changed from M2b's initial `[1.0, 2.0, 4.0, ...]` to start at 0.5s.

### Active Connection Tracking

For the shutdown log ("N active connections"), the app needs a count of active WS
proxy connections. Simple approach: an `asyncio.Event` or atomic counter incremented
on proxy_stream entry, decremented on exit. Stored in app state.

```python
# In app lifespan or module:
_active_ws_connections: int = 0
```

### Bounce Survival (minimal new code)

The orchestrator is stateless by design (M1/M2a). On every `GET /sessions` call, it
runs `docker ps` fresh. No session registry persists across restarts.

Verify at implementation time:
- The app lifespan teardown (if M2a added one for config) does NOT stop containers
- uvicorn's SIGTERM handling closes the HTTP/WS server but does not touch Docker
- The only subprocess calls on shutdown are "none" (verify no atexit hooks)

New code: startup log of `len(list_sessions())` in lifespan.

## Milestones

### 1. Add operational logging throughout the orchestrator

**Approach:**
- Add logging configuration (format, level, LOG_LEVEL env var) to the orchestrator
  package.
- Add log statements to existing files (created by M1/M2a/M2b):
  - `lifecycle.py`: INFO on session start/stop, WARNING on Docker errors
  - `proxy.py`: INFO on client connect/disconnect, WARNING on backend unreachable
  - `app.py` lifespan: INFO on startup (discovered session count), INFO on shutdown
    (active connection count)
  - `docker.py`: DEBUG for subprocess commands
- Add active WS connection counter (increment on proxy_stream entry, decrement on exit)
  for the shutdown log.
- Format: `HH:MM:SS LEVEL name  message` — matches uvicorn's output style.
- ⚠️ Don't log request bodies or sensitive data. Session IDs and workspace names are fine.

**Tasks:**
- Add logging configuration to orchestrator package
- Add INFO log to `lifecycle.py` start_session and stop_session
- Add INFO log to `proxy.py` proxy_stream (connect and disconnect)
- Add INFO log to `app.py` lifespan (startup discovery count, shutdown)
- Add DEBUG log to `docker.py` subprocess calls
- Add active WS connection counter for shutdown message
- Create `tests/test_orchestrator_logging.py`
- Test: session start produces INFO log containing session_id and workspace
- Test: session stop produces INFO log containing session_id
- Test: proxy connect/disconnect produce INFO logs
- Test: startup log contains discovered count
- Test: LOG_LEVEL=WARNING suppresses INFO messages
- Test: LOG_LEVEL=DEBUG shows subprocess commands

**Deliverable:** Orchestrator produces clear, light-touch operational logs at
appropriate levels.

**Verify:** `pytest tests/test_orchestrator_logging.py -v` — all pass.

---

### 2. Add error resilience (global handler + DockerError + WS safety)

**Approach:**
- Create `DockerError` exception class in `docker.py`.
- Wrap all `subprocess.run` calls in `docker.py` to raise `DockerError` on non-zero
  exit (replacing any bare returncode checks).
- Add global exception handler to Starlette app via
  `app.add_exception_handler(Exception, handler)`.
- Add try/except wrapper around the relay logic in `proxy_stream` (for unexpected
  errors only — existing error cases from M2b are already handled).
- Both handlers: extract file:line from traceback, log ERROR, return helpful message.
- Verify existing per-route error handling (KeyError → 404, ValueError → 400) is
  NOT intercepted by the global handler (Starlette dispatches handled exceptions
  before the fallback).
- ⚠️ The global handler must not swallow `SystemExit` or `KeyboardInterrupt`.

**Edge cases:**
- Docker daemon not running → DockerError with "Docker daemon unreachable" message
- Docker command timeout → subprocess.TimeoutExpired, wrapped in DockerError
- Unexpected exception in proxy relay → logged with origin, WS closed with 1011,
  other connections unaffected
- Malformed request body → 400 (already handled, not caught by global handler)

**Tasks:**
- Create `DockerError` exception class in `docker.py`
- Wrap all `subprocess.run` calls to raise `DockerError` on failure
- Add global exception handler to Starlette app
- Add try/except in `proxy_stream` for unexpected errors
- Verify `SystemExit`/`KeyboardInterrupt` are not caught
- Verify existing 404/400 handlers still work
- Create `tests/test_orchestrator_errors.py`
- Test: unexpected error in route → 500 response + logged with file:line
- Test: DockerError (daemon unavailable) → helpful error message in response
- Test: WS proxy unexpected error → connection closed 1011, logged
- Test: expected errors (404, 400) still work unchanged
- Test: second request succeeds after first request raised unexpected error

**Deliverable:** Orchestrator never crashes from a single-request failure; all errors
produce helpful messages with origin info; process stays running.

**Verify:** `pytest tests/test_orchestrator_errors.py -v` — all pass.

---

### 3. Tune reconnect timing and prove bounce survival (D4 + D9)

**Approach:**
- Adjust TUI backoff schedule: `[0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0]` (start at
  0.5s instead of 1.0s, 30s total cap). This ensures a local bounce (orchestrator
  restart takes <1s) reconnects on the first or second attempt.
- Write integration tests that simulate the orchestrator bounce:
  1. Orchestrator app starts, discovers sessions (mocked docker ps)
  2. App shutdown — verify no `docker stop` subprocess calls
  3. App restarts — `GET /sessions` returns same sessions
- Write TUI reconnect integration test:
  1. TUI connected via mocked WS
  2. WS closes (simulating orchestrator death)
  3. TUI retries at 0.5s, connects, fetches history
  4. Verify no duplicate turns rendered
- Verify agent connection-agnosticism:
  - Agent's `_broadcast()` with empty clients set → no error, no-op
  - `handle_message` task runs to completion with no connected clients
- Document manual e2e bounce test procedure (for host verification).
- ⚠️ Integration tests mock Docker subprocess — not real containers. Manual e2e is
  the true validation.

**Edge cases:**
- Orchestrator restarts instantly → reconnect at 0.5s succeeds (first attempt)
- Orchestrator takes 2s to restart → reconnect at 0.5s fails, 1.5s succeeds (second)
- Orchestrator down for >30s → TUI gives up, shows error
- Agent mid-turn when orchestrator bounces → turn completes, output buffered in
  session's broadcast queue (no connected clients to receive it, but turn is in
  history for catch-up)

**Tasks:**
- Adjust TUI backoff schedule in ws_client.py / tui reconnect logic
- Create `tests/test_orchestrator_bounce.py`
- Test: orchestrator shutdown does NOT call docker stop
- Test: after restart, GET /sessions returns same sessions (stateless rediscovery)
- Test: startup log shows "Discovered N running sessions"
- Test: TUI disconnect → 0.5s retry → reconnect → history fetch → no dups
- Test: TUI close code 4004 → immediate give-up (no retry)
- Test: agent broadcast to empty set → no error
- Document manual e2e procedure in plan (below)

**Deliverable:** D4 and D9 are proven via integration tests; reconnect timing is tuned
for sub-2-second local recovery.

**Verify:**
1. `pytest tests/test_orchestrator_bounce.py -v` — all pass
2. `pytest tests/ -v` — full suite, no regressions
3. Manual e2e (on host):
   - `archie serve` → `archie start` → attach TUI → observe working session
   - Ctrl+C the orchestrator → TUI shows "Reconnecting..."
   - `archie serve` again → TUI reconnects, conversation intact
   - `archie ls` → session still listed

---

## Not yet specified

- Structured JSON logging (for when orchestrator is wrapped in systemd)
- `serve stop` / `serve restart` / `serve reload` commands (deferred to systemd era)
- Clean session termination signal (graceful stop vs hard docker stop)
- Log rotation / file output (not needed for foreground process)
- Metrics on reconnect frequency / timing (observability improvement, future)
