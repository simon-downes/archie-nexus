# 019 — PROJECT: Orchestrator Control Plane

> **This is a PROJECT plan, not an implementation plan.** It is NOT implemented
> directly. Each milestone below is a **tracer-bullet / vertical slice** that will
> go through the full planning process and produce its own dedicated numbered plan
> (020+). This document tracks the decomposition, sequencing, and the load-bearing
> architectural decisions the slices must honour. Update the "Status" table as each
> slice's plan is written and landed.

## Objective

Introduce a **per-host orchestrator** as the control plane for archie-nexus. Move all
Docker lifecycle, session discovery, credential distribution, and metrics aggregation
out of the CLI and into a long-running `archie serve` process. Both the TUI and a
future web client become **pure protocol clients** of the orchestrator. This unblocks
remote/cross-host sessions and the web client while keeping the container simpler and
the system resilient to orchestrator failure.

Each milestone is a thin end-to-end slice (client → orchestrator → session/Docker),
independently shippable and dogfoodable, rather than a horizontal layer.

## Context

Today the CLI (`cli/src/archie_cli/cli.py`) shells directly to the local Docker daemon
for every operation (build/run/exec/stop/inspect/port) and discovers sessions by
parsing `docker ps`. Addressing is hardcoded to `127.0.0.1`; session ports are
published loopback-only. Credentials reach the container via a host `~/.nexus`
bind-mount. There is zero auth/TLS on any endpoint. The wire protocol
(`shared/src/archie_shared/events.py`, JSON over WS/HTTP, multi-client broadcast) is
already network-clean — transport is not the hard part; **discovery, addressing,
credentials, and lifecycle** are the local-Docker assumptions to unwind.

Full architecture facts, rationale, and the alternatives considered are recorded in
memory `archie-nexus-roadmap.md` (Wave 0 decisions). This project operationalises those
decisions.

## Locked architectural decisions (every slice MUST honour these)

- **D1 — Orchestrator is CONTROL PLANE + CLIENT INGRESS GATEWAY; never in the LLM
  egress path.** Two distinct "data paths" must not be conflated:
  - **Outbound LLM egress** (container → model provider): the orchestrator is NEVER in
    this path (see D2). The in-container ACP is the single metering point.
  - **Inbound client→session traffic** (TUI/web → session WS/HTTP): the orchestrator
    IS the single ingress point — a reverse proxy in front of every session (see D8).
  Beyond routing, the orchestrator owns lifecycle, discovery, the auth boundary, cred
  distribution, and metrics aggregation.
- **D2 — Direct LLM egress for ALL backends.** No proxying of *LLM/provider* traffic.
  The container is the single metering point (because ACP is in-container and bypasses
  any proxy). The orchestrator does not route LLM traffic. (This is about outbound
  egress only; inbound client traffic IS proxied — see D8.)
- **D3 — Sessions are ALWAYS co-located with their orchestrator's host.** Mounts
  (workspace, skills, home) are host-local. Remote = a remote *client*, never a remote
  workspace. This rule makes remote + metrics-pull fall out cleanly. (Note: this does
  NOT co-locate LLM *backends* — Ollama etc. may live on other hosts; see D7.)
- **D4 — Orchestrator lifecycle is DECOUPLED from session lifecycle.** Stopping,
  restarting, reloading, or crashing the orchestrator MUST NOT kill running sessions.
  Sessions continue autonomously, buffer their JSONL locally, and reconcile on the
  orchestrator's return. On restart the orchestrator rediscovers sessions via docker ps.
- **D5 — Metrics = LIVE CAPTURE from WS proxy frames → SQLite** (`~/.nexus/metrics.db`).
   Because the orchestrator is the single ingress proxy (D8), it observes every
   `Usage`/`SessionInfo`/`ModelSwitched` frame in real time and records
   per-request cost/usage into an indexed SQLite store. **JSONL (plan 005 write-only
   logs) remains the durable source of truth for conversation *content*;** SQLite is a
   metrics-specific derived index. Capture is observability only — it MUST NOT affect the
   relay data path (D1/D2); extraction failures are logged and swallowed.
   **Trade-off:** metrics are only captured while the proxy is running; frames during an
   orchestrator bounce are lost from metrics (negligible — clients can't reach a session
   without the orchestrator, D8). A JSONL-based reconciliation on startup is additive
   later if needed.

   > **Revision (2026-07-24):** original D5 was "PULL from co-located JSONL" (host-path
   > resolution via `home_dir()`, cli.py:230/273). Superseded by live WS-frame capture:
   > same token/cost data, captured at the proxy instead of parsed from disk. See 024.
- **D6 — Orchestrator entrypoint is explicit `archie serve`** (foreground; systemd/
  launchd wrap it later). No hidden auto-start.
- **D7 — LLM backend addressing is PER-MODEL catalog/config data; the orchestrator is
  NOT involved.** Each model's endpoint lives on its `ModelEntry` provider config
  (`OllamaProvider.endpoint`, models.py:46) and is overridable per-model via
  `<ARCHIE_HOME_DIR>/models.yaml` (models.py:174-183). Backends are NOT co-located with
  the session (D3 co-locates *sessions*, not backends): an Ollama model may run on a
  remote host (e.g. an EC2 GPU box) — that's just a routable `host:port` in the catalog,
  no Docker networking involved. For a **host-local** Ollama, the container reaches it
  via the existing `host.docker.internal` route (`--add-host=…:host-gateway`,
  cli.py:259) — **this route STAYS**; it is a legitimate container→own-host path, not a
  hack to remove.

  > **Correction (2026-07-22):** the earlier D7 ("remove `host.docker.internal`; the
  > orchestrator tells the container where host services live") was wrong on two counts:
  > (1) backends aren't necessarily host-local, and (2) endpoints are per-model catalog
  > data, not an orchestrator-injected global. `--add-host` is a *route* (not just a
  > default string) and is still needed for the co-located case. The old M4 slice built
  > on that mistaken premise and has been dropped (see M4 below).
- **D8 — Orchestrator is the SINGLE CLIENT INGRESS (reverse proxy), for local AND
  remote.** All client↔session traffic (`/stream`, `/status`, `/history`, `/shell`)
  flows client → orchestrator → session-over-loopback. Session ports STAY bound to
  loopback (`127.0.0.1:0:8080`, cli.py:261) and are NEVER published on a routable
  interface. This is what makes remote tractable: one known orchestrator port and one
  auth boundary, instead of a dynamic, ephemeral, ever-changing set of exposed session
  ports. **One common path** — local clients do NOT get a direct-loopback shortcut;
  local and remote use the identical proxied path (avoids a two-path split). Cost:
  every attached client (local included) is interrupted by an orchestrator restart —
  reconciled by D9.
- **D9 — Clients auto-reattach; sessions are CONNECTION-AGNOSTIC.** Because ingress is
  proxied (D8), an orchestrator bounce severs the client's live connection. The
  invariant that makes this safe: **the agent session does not care whether a client is
  connected.** The agent loop runs autonomously until it needs input, then simply waits
  — it never depends on a live client or a live orchestrator. Clients MUST detect
  connection loss and auto-reattach through the orchestrator, using `/history` catch-up
  (app.py:144) to replay missed turns. This generalises D4's survival guarantee to the
  *client connection* itself: session survival is unconditional; client reconnect is
  transparent.
- **D10 — Client↔orchestrator auth is DEFERRED (added 2026-07-24).** This is a personal
  tool on a trusted network; a token/mTLS boundary adds friction without current need. M6
  ships the orchestrator's routable front door WITHOUT auth. The topology (single routable
  ingress in front of loopback-only sessions, D8) is deliberately chosen so an auth
  middleware can be dropped in front of the ingress later as a purely additive change,
  without reworking session networking. Credential *delivery* to a remote orchestrator
  (`archie auth push` → `POST /credentials`) is separate from and not gated by client auth.

  > **Supersedes** the earlier "M6 = remote client **+ auth boundary**" framing, which
  > made auth load-bearing for M6. Auth is now its own future slice. This also removes the
  > contradiction with 022 (which must NOT add auth in front of the proxy).

## Milestones (tracer-bullet vertical slices → each gets its own plan)

Each slice states its vertical cut (what moves across client/orchestrator/session), the
decisions it proves, and its dedicated-plan scope. Requirements/Technical Design/Tasks
are produced when that slice's plan is written.

### M1 — Tracer bullet: `archie serve` + `ls` end-to-end

**Vertical cut:** stand up the orchestrator process and route the *single simplest*
operation through it, top to bottom.

- New `archie serve` command starts a long-running orchestrator (Starlette/ASGI,
  reuse the agent's server stack) on a fixed local port.
- Orchestrator exposes `GET /sessions` — does the `docker ps` parsing that
  `list_sessions()` (cli.py:122) does today, returns `list[SessionDescriptor]`.
- `archie ls` (`ls_cmd`, cli.py:302) calls the orchestrator API instead of Docker.
- Client fails gracefully with a clear message when the orchestrator isn't running.

**Proves:** D1 (control plane exists), D6 (`archie serve`), the client→orchestrator→
Docker path. Establishes where orchestrator code lives (new package? `agent/`? new
`orchestrator/` workspace member — decide in this slice's plan).
**Code-location risk to resolve in M1's plan:** "reuse the agent's server stack" is
non-trivial — that stack lives in `archie_agent` (the CONTAINER package). Reusing it
either creates a host→container package dependency or requires extracting shared server
infra into `archie_shared`. The new-`orchestrator/`-member option avoids the coupling at
the cost of scaffolding. This is a real fork, not a formality.
**Dogfood value:** none functional yet (ls behaves identically) — this is the
architecture-locking validation step.

> **M2 was split into M2a + M2b.** The original single "full lifecycle" slice was too
> large for one plan (`start` alone — cli.py:212–298 — carries port alloc, ready-wait,
> mount assembly, env injection, plus the unresolved `build` decision). The split
> preserves "one plan per milestone" while keeping each half a true vertical tracer
> bullet. Together M2a + M2b remove all direct Docker calls from `cli.py` (D1 fully
> locked). The seam: M2a owns **session lifecycle** (create/destroy a container); M2b
> owns **connecting a client to an existing session** (the piece that needs the
> returned endpoint / `SessionDescriptor.host`).

### M2a — Session lifecycle through the orchestrator (`start` + `stop`)

**Vertical cut:** move container create/destroy behind the API; the CLI stops running
`docker run` / `docker stop` itself.

- Orchestrator: `POST /sessions` (start = `docker run` with mount/env/ready-wait
  assembly, cli.py:252–286) and `DELETE /sessions/{id}` (stop, cli.py:416).
- CLI `start`/`stop` (cli.py:212/416) become API calls. `start --detach` returns the
  descriptor; non-detach still launches the local TUI against the returned endpoint.
- **Port-management strategy (DECIDE here):** today `start` binds ephemeral
  `127.0.0.1:0:8080` (cli.py:261) then reads the port back via `_query_port`
  (cli.py:66). Decide whether the orchestrator queries post-start (keep today's model),
  assigns pre-start, or uses a different networking model. Constrains M2b and M6.
- `build` (cli.py:170) — decide whether it stays a client-side/local op or moves
  behind the orchestrator (image is host-local; likely orchestrator-side). If it moves,
  it belongs in this slice's plan; if it stays client-side, note that explicitly.

**Proves:** D1 for the write path; the orchestrator owns container lifecycle.
**Dogfood value:** create and destroy sessions through the orchestrator (identical UX).

### M2b — Attach / exec routing (`attach` + `shell`) — CLI becomes pure client

**Vertical cut:** connect a client to an *already-running* session via the orchestrator,
removing the last direct Docker calls from `cli.py`.

- Orchestrator: attach-routing for an already-running session. **Per D8, the
  orchestrator is the ingress — it REVERSE-PROXIES the session's WS/HTTP
  (`/stream`, `/status`, `/history`, `/shell`) rather than handing the client a
  loopback endpoint to dial directly.** This is where the ingress proxy first appears;
  even locally the client talks to the orchestrator (single common path, D8). `shell`/
  exec routes through the same gateway (`POST /sessions/{id}/shell` or a proxied
  `/stream`-style channel).
- CLI `attach`/`shell` (cli.py:371/322) become orchestrator API/proxy calls; session
  resolution/prefix matching (`_pick_session`, cli.py:461) uses the M1 `GET /sessions`
  data.
- **Add `SessionDescriptor.host`** here (descriptor.py:17, already has `port` at :30).
  Because clients go through the proxy (D8), `host` describes where the *orchestrator*
  reaches the session (loopback), not an address the client dials directly. Stays
  `"127.0.0.1"`; M6 makes the *orchestrator's* front door routable, sessions stay
  loopback.
- **Client auto-reattach (D9) starts here:** the client must handle proxy connection
  loss by reconnecting through the orchestrator + `/history` catch-up (app.py:144).
  Hardened as a deliberate feature in M3.
- After this slice: **no direct Docker calls remain in `cli.py`** — CLI is a pure client.

**Proves:** D1 fully, D8 (ingress proxy first appears), D9 (auto-reattach begins).
Enforces "stop growing cli.py Docker logic."
**Dogfood value:** attach/shell go through the orchestrator; whole team on the client path.

### M3 — `serve stop/restart/reload` + session survival

**Vertical cut:** prove the lifecycle-decoupling invariant end-to-end.

- `archie serve stop|restart|reload`.
- On restart: orchestrator **rediscovers** running sessions via docker ps (no session
  killed). **Because ingress is proxied (D8), an orchestrator restart severs every
  attached client's live connection — local and remote.** D9 makes this a non-event:
  clients auto-reattach through the orchestrator and the container's `/history`
  catch-up (app.py:144) replays missed turns. This slice HARDENS the D9 auto-reattach
  path into a deliberate, sub-second-invisible feature (it is the everyday cost of the
  single common path, not a rare edge case).
- **Session connection-agnosticism (D9) is the load-bearing invariant here:** the agent
  loop runs autonomously through the disconnect — it only cares about a client when it
  needs input, then waits. Verify the agent loop genuinely blocks-for-input rather than
  requiring a live client, in M3's plan.
- `reload`: re-read config / model catalog / credentials without dropping the process
  or disturbing sessions.
- Container-side: the container is ALREADY orchestrator-unaware (standalone ASGI app,
  inbound WS, JSONL buffered locally regardless) — so orchestrator absence is
  *already* non-fatal. The genuine new container-side work here is the clean
  **termination signal** on the WS command set (finish current turn → flush → exit),
  distinct from a hard `docker stop`; plus confirming the host-side reconcile path
  (rediscover via docker ps, `/history` catch-up at app.py:144). Verify in M3's plan
  whether any real container change beyond the termination command is needed.

> **Descope (2026-07-24, per 023):** the spec DEFERS `serve stop/restart/reload` and the
> clean-termination WS signal. Rationale: the orchestrator is stateless (nothing to
> hot-reload — config/catalog/creds are read per-operation), so Ctrl+C + a later
> systemd/launchd wrapper covers stop/restart; `docker stop`'s SIGTERM is deemed graceful
> enough given per-event JSONL append. What 023 KEEPS as core: session survival across an
> orchestrator bounce + ephemeral-port re-discovery via docker ps, and the D9 invisible
> auto-reattach. The clean-termination signal is left for the Wave 1 steering/abort work
> (shared WS-command surface); note that coupling when planning Wave 1.

**Proves:** D4 (decoupling), D6 (operational commands), D9 (auto-reattach hardened).
Directly exercises the crash-survival property as a deliberate feature.
**Dogfood value:** restart the orchestrator to pick up changes without losing work.

### M4 — ~~Remove `host.docker.internal` (Ollama addressing)~~ — DROPPED

> **Dropped 2026-07-22.** This slice rested on the old (mistaken) D7: "remove
> `host.docker.internal`; the orchestrator injects the Ollama address." Two errors:
>
> 1. **LLM backends aren't necessarily host-local.** D3 co-locates *sessions* with the
>    orchestrator, not *backends*. An Ollama model may run on a remote host (e.g. an EC2
>    GPU box). Its address is just a routable `host:port` — no Docker networking involved.
> 2. **Endpoints are per-model catalog/config data, not an orchestrator-injected global.**
>    `OllamaProvider.endpoint` (models.py:46) is per-`ModelEntry`, overridable per-model
>    via `<ARCHIE_HOME_DIR>/models.yaml` (models.py:174-183). Different models can point
>    at different hosts. The orchestrator has no role here.
>
> Consequently `--add-host=host.docker.internal:host-gateway` (cli.py:259) **stays** — it
> is the legitimate container→own-host route for a *host-local* Ollama, not a hack to
> remove. See the reframed D7.
>
> **Residual work (small; fold elsewhere or drop):** confirm the per-model catalog +
> `models.yaml` override path cleanly expresses (a) a host-local Ollama (default
> `host.docker.internal:11434`) and (b) a remote Ollama (`ec2-host:11434`). This is
> catalog/config documentation, not an architectural slice, and needs no orchestrator
> involvement. No `019` slice owns it.

### M5 — Metrics aggregation (live WS-frame capture → SQLite)

**Vertical cut:** surface aggregate cost/usage without touching the data path.

- Orchestrator captures `Usage`/`SessionInfo`/`ModelSwitched` frames as they flow
  through the ingress proxy (D8) and records per-request cost/usage into SQLite
  (`~/.nexus/metrics.db`), on a background queue so the relay is never blocked.
- Orchestrator exposes an aggregate endpoint (e.g. `GET /metrics` or
  `GET /sessions/{id}/metrics`); TUI (and later web) can display totals.
- No container changes; extraction is observability only — a metrics failure MUST NOT
  affect the relay (D1/D2). JSONL remains the source of truth for conversation content.

**Proves:** D5 (revised), and that observability is NOT an architectural risk (a pure
proxy tap). JSONL reconciliation on startup is explicitly deferred.
**Dogfood value:** real cost visibility across sessions — a stated pain point.

### M6 — Remote client (auth deferred)

**Vertical cut:** the same client code, pointed at a non-local orchestrator.

- **The ORCHESTRATOR's front door becomes routable — NOT the session.** The client is
  pointed at a remote orchestrator address; the orchestrator reverse-proxies to the
  co-located session over loopback (D8). There is no `SessionDescriptor.host` (dropped,
  see D8 / forward-compat) — the orchestrator resolves the loopback target from the
  session's port, so nothing about the orchestrator→session hop changes here.
  `ArchieApp(host, port)` (parameterized; cli.py:297,410 pass 127.0.0.1 today) is
  pointed at the orchestrator's routable address, not the session's.
- **Session ports STAY loopback-only** (`-p 127.0.0.1:0:8080`, cli.py:261) — they are
  NEVER published routable. Remote reachability comes entirely from the orchestrator
  reverse-proxying client traffic to the co-located session over loopback (D8). This is
  the whole reason remote is tractable: no dynamic/ephemeral session-port exposure.
- Client targets a remote orchestrator via config **profiles** (`profile/workspace`,
  `profile/session-id`) with an `ARCHIE_HOST` env override — DECIDE details in the plan.
- **No client↔orchestrator auth (D10 — deferred).** M6 is a trusted-network personal
  tool; auth is explicitly out of scope for this project and lands as an additive
  token-middleware slice later. The bind geometry (single routable ingress, loopback
  sessions) is chosen so auth can be dropped in front of the ingress WITHOUT reworking
  the topology.
- Credential delivery to a remote orchestrator: explicit `archie auth push <profile>`
  → `POST /credentials` writes the orchestrator's own `~/.nexus/credentials.yaml`
  (atomic, 0600). **D1 boundary note:** distributing secrets does NOT put the
  orchestrator in the LLM/data path (D1 is about traffic routing, not secret custody).
- Make the client READ `protocol_version` (sent at app.py:275, currently ignored) and
  WARN on mismatch as part of hardening the now-networked boundary (full negotiation
  deferred).

**Proves:** D3 (co-location makes this tractable), D8 (single ingress gateway), D9
  (auto-reattach), cross-host with unchanged client logic. Carries the deferred
  remote-addressing decision.
**Ingress mechanism note:** the orchestrator reverse proxy for `/stream` (WS),
  `/status`, `/history`, `/shell` first appears in M2b. D3 co-location guarantees the
  target session is loopback-reachable from the orchestrator.
**Dogfood value:** drive a session on another box from the local TUI.

> **Web client** (separate future project) becomes a second consumer of the M1–M6 API.
> It is intentionally out of scope here — this project makes it a "just another
> protocol client" problem rather than an architectural one.
>
> **Not to be confused with M7 (below).** The *web client* drives sessions (attach,
> stream, steer) like the TUI. The *orchestrator web console* (M7) is a read-only face
> on the orchestrator's own control-plane state — it is NOT a session client, cannot
> start/attach/stop. Different concern, different slice.

### M7 — Orchestrator web console (read-only session list)

**Vertical cut:** put a human-friendly HTML face on the orchestrator's own
control-plane state, served by the orchestrator's ASGI app over the M1 discovery data.

- Add an HTML route (and, if needed, `StaticFiles`) to the **orchestrator's** Starlette
  app (the one M1 stands up — NOT the agent/container app at
  `agent/src/archie_agent/app.py`, which serves per-session `/status`, `/history`,
  `/shell`, `/stream`). Renders the current sessions from the same data `GET /sessions`
  (M1) returns.
- **Read-only and localhost-only.** No start/attach/stop; no writes; no remote exposure.
  It is an orchestrator/host console, not a session client — it renders control-plane
  state the orchestrator already owns.
- Intended as a starting hook: a debug/console surface for when the CLI/TUI client is
  broken, and the seat for future host-focused read-only views.

**Proves:** D1 — a pure control-plane consumer that never touches the LLM/data path and
never attaches to a session. Because it is read-only + localhost-only, it does NOT
cross the M6 auth boundary and does NOT depend on M6.
**Constraint (load-bearing — keeps this slice independent of M2a/M6):** the read-only +
localhost-only cut is what lets M7 land right after M1. The moment the console wants to
*control* a session (stop/kill) it inherits M2a (lifecycle write path); the moment it
wants to be reachable remotely it inherits M6 (auth boundary). Future build-out of those
capabilities is a *later* slice / the web-client project, not an in-place expansion of M7.
**Distinct from the web client** (roadmap #4): that is a full session-driving protocol
client equivalent to the TUI and depends on M1–M6 + auth. M7 is neither.
**Dependency:** M1 only (needs `GET /sessions` and the orchestrator ASGI app). Inherits
M1's server-stack / code-location decision (agent-stack reuse vs new `orchestrator/`
member) — does not reopen it.
**Dogfood value:** glance at running sessions in a browser; a fallback console when the
TUI misbehaves.

## Sequencing & dependencies

- M1 → M2a → M2b are the architecture-locking chain (do first; zero UX change).
  After M2b, `cli.py` has no direct Docker calls.
- M3 depends on M2a (needs orchestrator-owned lifecycle to restart around).
- ~~M4~~ **dropped** (see M4 section) — backend addressing is per-model catalog/config,
  not an orchestrator concern; `--add-host` stays.
- M5 depends on M2a *conceptually* (independent otherwise).
- M6 depends on M2b (pure-client CLI + the ingress proxy; no `SessionDescriptor.host` —
  the orchestrator resolves loopback targets from `.port`) and benefits from M3
  (survival + hardened auto-reattach); carries the deferred remote-addressing decision.
  **Note (D8/D10):** M6 makes the *orchestrator's* front door routable but does NOT add
  auth (deferred, D10) and does NOT expose session ports — the reverse-proxy path itself
  is built in M2b.
- M7 depends on M1 only (read-only console over `GET /sessions`); independent of
  M2a–M6. Can land any time after M1. Its read-only + localhost-only cut is what keeps
  it independent of M6 and the M2a write path.

## Cheap-now forward-compat (fold into the earliest relevant slice)

- `SessionDescriptor.host` field — **DROPPED.** D8 (ingress proxy) means clients never
  dial sessions directly; the client already knows the orchestrator address. The
  orchestrator resolves session targets internally from `SessionDescriptor.port`.
- **Ingress reverse-proxy** — first appears in M2b (client→orchestrator→session for
  local); M6 puts a routable orchestrator address in front of it (auth deferred, D10).
  Sessions never publish routable ports (D8).
- **Client auto-reattach (D9)** — begins in M2b, hardened into an invisible feature in
  M3.
- Client `protocol_version` warning on mismatch (M6 hardening; full negotiation deferred).
- "Stop growing cli.py Docker logic" — enforced by M1/M2a/M2b.
- **Port-management strategy** — decided in M2a, constrains M2b + M6.

## Status (update as slices are planned/landed)

| Slice | Dedicated plan | Status |
|-------|----------------|--------|
| M1 — serve + ls tracer      | `020-spec-serve-and-ls.md` | planned |
| M2a — session lifecycle (start/stop) | `021-spec-session-lifecycle.md` | planned |
| M2b — attach/exec routing (attach/shell) | `022-spec-attach-exec-proxy.md` | planned |
| M3 — serve stop/restart/reload + survival | `023-spec-operational-hardening.md` | planned |
| ~~M4~~ — dropped (backend addr is catalog data; --add-host stays) | — | dropped |
| M5 — metrics (WS-frame capture → SQLite) | `024-spec-metrics-aggregation.md` | planned |
| M6 — remote client (auth deferred, D10) | `025-spec-remote-client.md` | planned |
| M7 — orchestrator web console (read-only) | `026-spec-web-console.md` | planned |

## Open decisions (resolved within specific slices)

- Where orchestrator code lives: new `orchestrator/` workspace member vs. reuse
  `agent/` server stack (M1).
- `build` stays client-side vs. orchestrator-side (M2a).
- Remote addressing scheme: `archie --host <addr>` flag vs. named config profiles (M6).
- Auth mechanism: DEFERRED (D10) — its own future slice, not part of M6.
- Orchestrator state: stateless-rediscover (leaning) vs. small persisted registry (M1/M3).
- Port management: query-post-start (today's model) vs. assign-pre-start vs. different
  networking model (M2a). Note: session ports stay loopback-only regardless (D8) — this
  decision is about how the orchestrator *discovers/assigns* the loopback port, not
  about routable exposure.
- Ingress reverse-proxy implementation: how the orchestrator proxies WS (`/stream`) +
  HTTP (`/status`, `/history`, `/shell`) to the co-located session, and how the client
  detects loss + auto-reattaches (D8/D9). First cut in M2b; routable (unauth) in M6.
- Agent-server-stack reuse: import from `archie_agent` (host→container coupling) vs.
  extract shared server infra to `archie_shared` vs. standalone in a new
  `orchestrator/` member (M1).
