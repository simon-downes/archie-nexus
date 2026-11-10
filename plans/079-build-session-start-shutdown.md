# 079 — Session startup and shutdown UX

Status: building
Branch: build-it/079-session-start-shutdown (base: main @ e6a1ac0)
Worktree: /workspace/.git/build-it-worktrees/079-session-start-shutdown

## Objective

Give `archie start` visible feedback while the orchestrator performs the existing blocking
container/Kiro startup, without changing the session API or event protocol. Make interactive
session termination orderly so the TUI closes client transports before the orchestrator removes
the container, and expected backend disconnects do not become unhandled proxy errors.

## Context

The user observed that starting a Kiro session takes noticeable time but produces no terminal
output before the main TUI appears. The CLI currently waits synchronously for `POST /sessions`;
the orchestrator returns only after Docker starts the agent and `/status` succeeds, while Kiro
initializes inside the container. Kiro stderr is consumed by `agent/src/archie_agent/kiro/acp.py`
and written to agent logs, so it is not currently a safe or structured client-facing progress
stream.

The user selected a simple activity indicator rather than a new staged startup-status protocol.
The existing API and wire contracts will therefore remain unchanged.

The user also reported shutdown errors. `cli/src/archie_cli/tui/app.py` currently sends DELETE
before setting its shutdown state and closing the WebSocket. This allows deliberate container
teardown to race with the TUI receive/reconnect loop and `/events` history requests. The
orchestrator proxy currently handles connection refusal and timeouts, but an HTTP backend
`RemoteProtocolError` escapes as a 500 and a WebSocket backend protocol loss is logged as an
unexpected internal proxy error.

## Baseline

- Check commands: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test`; `scripts/dev ruff check .`
- Setup: `scripts/dev sync`
- Setup leftovers: `.venv-archie/` is ignored local setup state; no tracked or copied setup files
- Failing before this work: none with the safe test environment; 4,715 tests passed in 138.26s and Ruff passed
- Not included: none; the first inherited-environment full test invocation was interrupted before a result while this session exported `ARCHIE_BACKEND=kiro` and `ARCHIE_SESSION_ID`.

## Decisions

| # | Decision | Chosen | Rejected | Why | Source |
|---|---|---|---|---|---|
| D1 | Startup feedback mechanism | Client-side activity indicator around the existing blocking POST | New asynchronous startup endpoint, polling API, SSE stream, or raw Kiro stderr forwarding | Preserves the current orchestrator contract and avoids exposing ACP/Kiro output; the user explicitly selected the simple indicator | user Q1 |
| D2 | Indicator implementation | Reuse the already-declared Rich dependency and its terminal-aware status display | Add a dependency; hand-written animation thread; plain one-shot output only | Rich is already a CLI dependency and supports animated TTY output with a non-interactive fallback | self |
| D3 | Indicator coverage | Show feedback for native and Kiro starts, with backend-specific wording | Kiro-only indicator | The same blocking lifecycle affects both backends, and one shared path avoids inconsistent CLI behavior | self |
| D4 | Quit ordering | Mark shutdown, cancel/await receive and reconnect tasks, close WebSocket, then DELETE | Delete first; close only after DELETE; leave reconnect tasks untracked | The current delete-first order is the observed race; client-first teardown prevents new requests during container removal | self |
| D5 | Proxy failure classification | Return controlled 502 for HTTP transport loss and use session-unreachable handling for expected WebSocket backend loss | Let transport exceptions reach Starlette as 500; swallow all proxy exceptions | Backend disappearance is expected during teardown, but genuinely unexpected proxy failures must remain visible | self |
| D6 | Test environment | Run repository checks with `ARCHIE_BACKEND` and `ARCHIE_SESSION_ID` unset | Inherit the surrounding Archie session environment | The agent backend is selected by environment and can alter lifespan tests; the repository conftest currently does not clear those variables | self |

## Requirements

- MUST display startup activity before the blocking `POST /sessions` request completes.
  - AC: CLI start tests observe the status context entered before the mocked POST and finalized after both successful and failed responses.
- MUST use backend-specific wording for startup activity while retaining a non-interactive fallback.
  - AC: native and `--kiro` CLI tests assert `Starting session` and `Starting Kiro session` respectively; captured/non-TTY output contains a readable status rather than requiring ANSI animation.
- MUST preserve the existing `POST /sessions` request body, timeout, response decoding, session output, detach output, and TUI launch behavior.
  - AC: existing `tests/test_cli_start_stop.py` assertions continue to pass, including the Kiro backend request and TUI arguments.
- MUST prevent the TUI from scheduling or retaining reconnect/history work after shutdown begins.
  - AC: quit tests set an active reconnect task with an in-flight `/events` replay and verify the reconnect/replay task is cancelled/awaited, no reconnect or second history request is started, and the receive task is also cancelled before deletion.
- MUST close the TUI WebSocket before requesting session deletion when termination is selected.
  - AC: the quit test records call order and observes WebSocket disconnect before HTTP DELETE; non-terminating quit still disconnects without DELETE.
- MUST treat an HTTP backend disconnect, including `httpx.RemoteProtocolError`, as a controlled session-unreachable response rather than an unhandled exception.
  - AC: proxy tests inject `RemoteProtocolError` and receive HTTP 502 with the existing session-unreachable error payload.
- MUST preserve timeout responses as 504, initial backend refusal as the existing WebSocket session-unreachable close, and unexpected proxy exceptions as logged 1011 internal errors.
  - AC: existing timeout/refusal/unexpected-error tests pass and new protocol-loss coverage verifies the expected classification.
- MUST document that startup remains synchronous, Kiro raw output remains in logs, and client transports close before container teardown.
  - AC: `docs/architecture.md` and `docs/kiro.md` describe the implemented behavior without claiming a startup progress protocol.
- MUST run tests and lint with `ARCHIE_BACKEND` and `ARCHIE_SESSION_ID` unset.
  - AC: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` and `scripts/dev ruff check .` pass for the completed change.

Out of scope: detailed Kiro startup phases; raw Kiro stderr/ACP streaming; asynchronous session creation; new public routes or event schemas; automatic restart/resume; changing the orchestrator's lifecycle timeout; Docker or production changes.

## Design

### Overview

The CLI wraps its existing synchronous session-create call in a Rich status context. The TUI
owns explicit task shutdown: it sets the shutdown guard first, cancels tracked receive/reconnect
work, closes the WebSocket, and then optionally deletes the session. The orchestrator proxy
maps expected backend transport loss to controlled responses while retaining diagnostics for
unexpected failures.

### Stack

No new dependency. Reuse Rich already declared by `cli/pyproject.toml`; follow the existing
Click command structure in `cli/src/archie_cli/cli.py` and Rich usage in `cli/src/archie_cli/stats.py`.
Use the existing `httpx`, `websockets`, Starlette and pytest infrastructure.

### Code structure

- `cli/src/archie_cli/cli.py`: startup status helper/context around the existing POST.
- `cli/src/archie_cli/tui/app.py`: reconnect-task ownership and client-first quit ordering.
- `orchestrator/src/archie_orchestrator/proxy.py`: HTTP and WebSocket expected transport handling.
- `tests/test_cli_start_stop.py`: startup indicator behavior.
- `tests/test_tui_quit.py`: shutdown order and task cancellation.
- `tests/test_orchestrator_proxy.py` and `tests/test_orchestrator_ws_proxy.py` or
  `tests/test_orchestrator_errors.py`: transport classification.
- `docs/architecture.md`, `docs/kiro.md`: lifecycle/debugging documentation.

### Data flow

Successful start: CLI enters status → POST blocks while orchestrator starts the container →
status finalizes → existing descriptor handling prints the session and attaches/detaches.
Failure: CLI enters status → POST raises or returns an error → status finalizes → existing
Click error handling reports the failure.

Terminating quit: TUI sets `_shutting_down` → cancels/awaits receive and reconnect tasks →
closes the client WebSocket → DELETE reaches the orchestrator → orchestrator stops the container
→ TUI exits. A DELETE 404 remains accepted because the session may already have stopped.

### Error handling

- Rich status cleanup occurs on both success and exception; no progress task is allowed to outlive
  the blocking request.
- Shutdown cancellation is awaited and cancellation exceptions are contained; shutdown does not
  trigger reconnect or client-facing connection-loss notices.
- HTTP timeout remains 504. Other HTTP transport failures from a resolved session return 502.
- WebSocket connection refusal/OSError retains code 4002. Expected backend protocol closure uses
  the session-unreachable path without an internal traceback. Unexpected exceptions retain code
  1011 and error logging.

### Non-functional

The change adds no network calls, dependencies, persistence, or protocol fields. The startup
indicator must not print credentials, ACP frames, or raw backend stderr. Test commands explicitly
unset session-only backend variables so agent lifespan tests cannot accidentally launch real Kiro.

## Milestones

### [x] M1 — Show activity during session startup

- **Approach:** Reuse Rich's terminal-aware status context in `cli/src/archie_cli/cli.py` around the existing synchronous `httpx.post` call. Keep status output separate from the existing session descriptor output and preserve the current 60-second timeout and error branches. Add focused tests to the existing CLI start test module, using the current HTTP mocks and a patched status context to prove ordering and cleanup.
- **Test seam:** Click invocation of `main start`, with the orchestrator HTTP POST mocked.
- **Edge cases:** POST raises → status is finalized and the existing orchestrator-unreachable Click error is shown; HTTP error response → status is finalized before the existing error; `--detach`/TUI success → existing output is unchanged apart from startup feedback.
- **Deliverable:** `archie start` visibly reports that startup is in progress before the TUI or detach details appear.
- **Verify:** `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test tests/test_cli_start_stop.py` passes, including new indicator tests.
- **Outcome:** Added Rich status feedback with native/Kiro wording around the existing POST; focused CLI tests verify backend labels and cleanup ordering on success/failure. Verified with `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test tests/test_cli_start_stop.py` (13 passed) and Ruff on both changed Python files.

### [x] M2 — Make TUI termination client-first

- **Approach:** Add an owned reconnect-task reference in `cli/src/archie_cli/tui/app.py`; have `_schedule_reconnect` store it and clear it when complete. In `_finish_quit`, set `_shutting_down` before cancellation, cancel/await receive and reconnect tasks, disconnect the WebSocket, then perform the existing DELETE when requested. Keep the existing accepted DELETE statuses and error reporting for actual deletion failures. Extend `tests/test_tui_quit.py` with an ordered fake/side-effect assertion and active reconnect cancellation coverage.
- **Test seam:** Textual's public `ArchieApp.action_quit` confirmation path, driven with the existing Textual test harness; the test may use injected fake transport/tasks to observe the teardown ordering.
- **Edge cases:** quit without termination → disconnect and exit, no DELETE; DELETE returns 404 → exit normally; DELETE transport/status failure after client teardown → report the existing termination error; reconnect with an in-flight history replay → cancellation stops the replay and prevents a follow-up request; reconnect is active → cancellation prevents another connect.
- **Deliverable:** terminating the TUI closes all client-side session activity before container deletion and never starts a reconnect during shutdown.
- **Verify:** `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test tests/test_tui_quit.py tests/test_tui_canonical.py` passes.
- **Outcome:** Added tracked reconnect-task ownership and client-first shutdown ordering. Tests drive the public quit action, verify WebSocket disconnect precedes DELETE, and verify receive/in-flight history tasks are cancelled with no reconnect. Verified with `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test tests/test_tui_quit.py tests/test_tui_canonical.py` (53 passed) and Ruff on the changed files.

### [x] M3 — Handle expected orchestrator backend loss cleanly

- **Approach:** Update `orchestrator/src/archie_orchestrator/proxy.py` after timeout handling to map HTTP transport loss, including `RemoteProtocolError`, to the existing 502 session-unreachable response. Add an explicit expected WebSocket protocol-loss branch alongside the existing refusal/OSError branch, while retaining the catch-all 1011 path for unexpected exceptions. Add focused proxy tests, then update `docs/architecture.md` and `docs/kiro.md` to describe the unchanged synchronous startup and ordered shutdown behavior.
- **Test seam:** Orchestrator ASGI HTTP proxy route and WebSocket proxy route with mocked Docker session resolution and backend transports.
- **Edge cases:** backend disappears during `/events` → 502, not Starlette 500; backend disappears while opening/relaying WebSocket → session-unreachable close and no internal-error traceback; timeout → 504; unexpected exception → 1011 and error log.
- **Deliverable:** normal session teardown no longer produces unhandled proxy errors while real proxy faults remain diagnosable.
- **Verify:** `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test tests/test_orchestrator_proxy.py tests/test_orchestrator_ws_proxy.py tests/test_orchestrator_errors.py` passes, followed by `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` and `scripts/dev ruff check .`.
- **Outcome:** Mapped all HTTP transport errors, including `RemoteProtocolError`, to controlled 502 responses while retaining 504 timeouts; classified expected WebSocket protocol loss as session-unreachable and preserved 1011 handling for unexpected errors. Added exact regression tests and documented startup/shutdown behavior. Focused proxy tests passed: 19 passed with the repository's existing Starlette deprecation warning.

## Deviations

- None yet.

## Risks

- Rich's captured-output rendering may differ between Click's test runner and a real non-TTY; the status helper tests will assert readable fallback behavior rather than terminal escape sequences.
- The initial inherited-environment test run was interrupted before pytest reported a result. The safe baseline with session-only variables unset passed the full suite.

## Review

- Plan review: one pass; resolved the M2 private test-seam concern by specifying the public `ArchieApp.action_quit` confirmation path, and expanded M2 acceptance/edge-case coverage to include cancellation of an in-flight `/events` replay.
- Code review: pending
- Merge risk: pending

## Outcome

Pending implementation and review.

PR: pending
CI: pending

## Follow-ups

- A staged startup-status API can be considered separately if a generic activity indicator is insufficient.
