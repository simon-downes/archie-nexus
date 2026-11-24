# 097 — Agent gateway

Status: planning
Branch: build-it/097-agent-gateway (base: main @ 2e8b9cf)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/097-agent-gateway

## Objective

Give every agent container a deliberately restricted, stable path back to the host orchestrator. The main orchestrator listener remains the operator-facing service; agents use a second gateway listener that permits health checks and agent-specific routes without exposing the rest of the orchestrator API.

The change must work on both Colima/Docker Desktop and native Linux Docker. It must preserve the existing agent sync protocol and make future agent-originated endpoints live under an explicit `/api/agent/` boundary.

## Context

The agent currently receives `ARCHIE_ORCHESTRATOR_URL=http://host.docker.internal:<orchestrator-port>`. On Colima, that address reaches the loopback-bound orchestrator, so an agent can address the entire HTTP API. On native Linux, `host-gateway` resolves to the Docker bridge gateway instead, so the same loopback-bound listener is unreachable and the agent exits during startup.

Previous sessions used the direct path from inside containers for development diagnostics, including `GET /health` and latency checks while implementing the agent config/credential sync protocol. The new gateway must retain that useful health probe while making the broader exposure intentional. The agent code already treats the URL as an opaque base URL and appends the sync path; no platform-specific networking belongs in the agent.

The orchestrator is a Python 3.13 Starlette/Uvicorn application. `archie serve` currently starts one Uvicorn listener, `orchestrator/src/archie_orchestrator/lifecycle.py` constructs the container environment, and `orchestrator/src/archie_orchestrator/agent_api.py` owns the authenticated `/api/agent/sync` route. Docker publishes each agent on a host loopback port, so orchestrator-to-agent traffic is a separate path and is not part of this change.

## Baseline

- Check commands: `scripts/dev test`; `scripts/dev ruff check .`; `scripts/dev ruff format .`
- Setup: `scripts/dev test` and `scripts/dev ruff check .` run in the worktree; no config copied
- Setup leftovers: none
- Failing before this work: `scripts/dev ruff check .` passed. `scripts/dev test` collected 5764 tests but failed in existing environment-dependent Kiro/agent WebSocket and question/host-config tests, including missing Kiro credentials; no gateway files were changed.
- Not included: the user's uncommitted `plans/060-spec-container-aws-profiles.md` and `plans/070-build-nexus-plugins.md` remain outside this worktree.

## Decisions

| # | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | How should agents reach the orchestrator? | Use an agent gateway on both Colima/Docker Desktop and native Linux; inject the gateway URL into every container | Direct main API on macOS; gateway only on Linux; bind the full orchestrator to all interfaces | A single agent-facing boundary avoids platform-dependent security and behaviour; the main API stays private | user Q1 |
| D2 | Which gateway routes are exposed? | `GET /health` and any method under `/api/agent/` | Transparent proxy for the entire API; only the current `/api/agent/sync` route | Health supports container diagnostics; the prefix gives future agent features a stable boundary without gateway edits for each new route | user Q1 |
| D3 | Where does the gateway run? | In the existing host orchestrator process as a second Uvicorn/Starlette listener | A separate proxy process; an orchestrator container | Reuses the existing runtime, logging and lifecycle without adding a process or Docker-socket/filesystem redesign | self |
| D4 | How is the gateway target selected? | Bind it to `127.0.0.1` on VM-backed macOS runtimes and to the native Docker bridge gateway on Linux; keep the container hostname `host.docker.internal` | Bind the main API to `0.0.0.0`; hard-code one Linux bridge address; add platform logic to the agent | The host process can bind the address appropriate to its runtime while the agent receives one stable URL shape | self |
| D5 | How is traffic forwarded? | Route-restricted HTTP forwarding from the gateway to the main listener over loopback, preserving method, path, body and relevant headers | Share the main app's complete route table; duplicate agent handlers in a second app | The gateway remains a real exposure boundary while the existing main handlers remain authoritative | self |
| D6 | Gateway port | Use the configured orchestrator port plus one (default `7601`) | Reuse the main port; ephemeral port; new user config field | A deterministic adjacent port makes the injected URL and diagnostics predictable without expanding configuration | self |

## Requirements

- MUST keep the existing operator-facing orchestrator listener and its configured host/port behaviour.
  - AC: existing serve and profile tests continue to assert the configured main listener.
- MUST start a gateway listener alongside the main orchestrator on every supported runtime.
  - AC: serve startup creates both listeners; a gateway port collision fails before the server starts with an actionable error.
- MUST expose `GET /health` through the gateway and return the same liveness response as the main service.
  - AC: a gateway client receives HTTP 200 and `{"status":"ok"}` for GET `/health`.
- MUST forward any HTTP method under `/api/agent/` through the gateway while rejecting paths outside `GET /health` and that prefix.
  - AC: gateway tests cover GET/POST/PUT/DELETE forwarding under `/api/agent/`, reject `/api/sessions` and non-GET `/health`, and preserve upstream status/body.
- MUST preserve the existing per-session bearer token and agent sync behaviour.
  - AC: the existing agent sync API tests pass through the gateway with a valid token and still return 401 for an invalid token.
- MUST inject the gateway URL, not the main orchestrator URL, into every new agent container.
  - AC: lifecycle tests assert the container receives `http://host.docker.internal:<main-port+1>` for default and custom ports.
- MUST resolve the gateway bind address without adding platform-specific networking code to the agent.
  - AC: address-selection tests choose loopback for non-Linux and the discovered Docker bridge gateway for Linux; discovery failures are surfaced before sessions start.
- MUST leave orchestrator-to-agent traffic on the existing host-loopback-published agent ports.
  - AC: existing proxy and lifecycle tests remain unchanged in their agent target assertions.
- MUST document the two listener roles, gateway allowlist, runtime address selection, and future `/api/agent/` extension rule.
  - AC: `docs/architecture.md` describes both directions and the gateway contract.

Out of scope: containerizing the orchestrator; changing the agent HTTP client protocol; exposing operator/control-plane routes through the gateway; adding authentication to currently host-local operator routes; changing Docker agent port publishing; adding a new user-facing configuration section; support for remote orchestrators or non-Docker runtimes.

## Design

### Overview

`archie serve` owns two listeners. The main listener serves the existing Starlette application on the configured profile host/port. The gateway listener serves a small route-filtering ASGI application on the runtime-specific host and `profile.port + 1`. It forwards allowed requests to the main listener over `127.0.0.1:<profile.port>` using the existing `httpx` dependency. `prepare_start` injects the gateway URL into the agent environment.

### Stack

No new dependency. Reuse Starlette, Uvicorn, HTTPX and the existing Docker CLI helpers. Programmatic Uvicorn servers run concurrently in the existing `archie serve` process so shutdown and log handling remain together.

### Code structure

- New `orchestrator/src/archie_orchestrator/agent_gateway.py`: allowed-path ASGI/Starlette application and HTTP forwarder.
- `cli/src/archie_cli/cli.py`: serve startup, listener preflight and concurrent server lifecycle.
- `orchestrator/src/archie_orchestrator/docker.py`: native Docker bridge gateway discovery.
- `orchestrator/src/archie_orchestrator/lifecycle.py`: use the gateway port when constructing `ARCHIE_ORCHESTRATOR_URL`.
- `tests/`: gateway route/forwarding tests, address discovery tests, serve/lifecycle regressions.
- `docs/architecture.md`: runtime topology and agent-facing API contract.

### Data flow

1. `archie serve` reads the configured main profile and derives the gateway port.
2. On Linux it discovers the Docker `bridge` network gateway; on non-Linux it selects host loopback, matching the verified Colima/Docker Desktop path.
3. It preflights both sockets, then starts the main app and route-filtered gateway concurrently.
4. `prepare_start` passes `http://host.docker.internal:<gateway-port>` to the container. The existing `--add-host=host.docker.internal:host-gateway` remains.
5. The agent sends health or `/api/agent/*` requests to the gateway. The gateway forwards only allowed requests to the main loopback listener and returns the upstream response.
6. The orchestrator continues to reach agent HTTP/WebSocket endpoints via their existing `127.0.0.1:<published-port>` mapping.

### Error handling

- Main or gateway bind collision: fail before starting either server and name the affected address/port.
- Docker bridge discovery failure on Linux: fail `archie serve` with the Docker command/error and explain that the gateway endpoint cannot be established.
- Disallowed gateway method/path: return HTTP 404 without forwarding.
- Upstream unavailable or malformed: return a bounded 502 response and log the failure without logging authorization headers or request bodies.
- Agent token validation remains in the existing `/api/agent/*` handlers; the gateway does not weaken or replace it.

### Non-functional

The gateway MUST not log bearer tokens or request bodies. It MUST not expose the main app's `/api/sessions`, auth, pages, metrics, or WebSocket routes. The deterministic adjacent port MUST be included in startup diagnostics so container connectivity failures can be distinguished from main-listener failures.

## Milestones

### [ ] M1 — Run the agent-facing gateway on both runtimes

- **Approach:** Add the route-filtered gateway and bridge-address discovery, start it beside the existing Uvicorn app, inject its URL into lifecycle-built containers, and cover the public gateway boundary with in-process HTTP tests. Follow the current serve preflight pattern in `cli/src/archie_cli/cli.py` and lifecycle command tests in `tests/test_orchestrator_lifecycle.py`. Keep `/api/agent/` forwarded to the existing handlers rather than duplicating them. ⚠️ The full suite has pre-existing credential-dependent failures; targeted gateway/lifecycle tests must pass independently.
- **Test seam:** Gateway HTTP client boundary plus `prepare_start`'s generated Docker environment and serve listener configuration.
- **Edge cases:** occupied main or gateway port → fail before server startup; missing Linux Docker bridge → actionable startup error; `/health` with POST → 404; `/api/sessions` with any method → 404; upstream sync 401 → 401 remains visible; upstream connection failure → bounded 502 without secrets in logs.
- **Deliverable:** New agents on Colima and native Linux are configured for the gateway; GET `/health` and all methods under `/api/agent/` work through it; the rest of the orchestrator API is unreachable through the gateway.
- **Verify:** `scripts/dev test tests/test_agent_gateway.py tests/test_orchestrator_lifecycle.py tests/test_orchestrator_cli.py tests/test_agent_sync_api.py`; `scripts/dev ruff check .`; `scripts/dev ruff format --check .`; inspect generated lifecycle command for the gateway URL and run the gateway TestClient assertions.
- **Outcome:** pending

## Deviations

- None.

## Risks

- A real native-Linux Docker smoke test is not available in this container; address discovery and HTTP forwarding will be unit-tested, while live Docker connectivity remains a deployment check.
- Running two Uvicorn servers in one process changes serve lifecycle code and must preserve signal handling and clean shutdown.

## Review

- Plan review: pending
- Code review: pending
- Merge risk: pending

## Outcome

Pending implementation.

PR: pending
CI: pending

## Follow-ups

- Consider an explicit gateway port setting if `profile.port + 1` conflicts with another service in real deployments.
- Add a native-Linux Docker integration test when a Docker daemon is available in CI.
