# Roadmap

_What this roadmap optimises for: a daily-driver personal agent, on an architecture that
won't need re-cutting when remote/web sessions arrive._

Living index of projects for archie-nexus. Entries graduate into numbered project/spec
plans in this directory. Detailed status lives in each project plan, not here.

## Now

1. **Orchestrator control plane** — move host-side session lifecycle, discovery, and
   client addressing out of `cli.py` into a per-host `archie serve` orchestrator; clients
   (TUI, later web) become pure protocol clients.
   - Scope: control-plane only (lifecycle/discovery/routing/auth-boundary/cred-distribution/
     metrics-aggregation). NOT in the LLM/data path. Locks D1–D7. Decomposed into slices
     M1→M2a→M2b (architecture-locking, zero UX change), then M3–M6.
   - Depends on: nothing (builds on shipped plans 003–005).
   - Status: in progress → `019-project-orchestrator-control-plane.md` _(file currently
     named `019-epic-...`; rename pending)_

## Next

2. **Daily-productivity quick-wins** — sharpen the everyday TUI/agent-loop experience.
   - Scope: `/skill` auto-load, `/clear`, `@` file-picker, mid-turn steering,
     end-of-turn summary, cost line, wire Esc/abort. All live in TUI + agent loop +
     session JSONL — none touch lifecycle/discovery/addressing.
   - Depends on: nothing (independent of #1; can run in parallel). ⚠️ Shares the WS command
     set with #1's M3 clean-termination signal — agree the abort/steering/termination
     vocabulary once, not twice.
   - Status: not started

## Later

3. **Remote sessions + auth boundary** — reach an orchestrator on another host; add the
   one auth/TLS boundary; `SessionDescriptor.host` becomes routable; negotiate
   `protocol_version`.
   - Scope: remote *client* to a remote orchestrator (workspace stays co-located per D3).
     Carries the deferred decisions: remote addressing scheme, auth mechanism (browser
     can't set WS headers → cookie/query-param).
   - Depends on: #1 (specifically slices M2b + M6; benefits from M3). Must honour D1–D7.
   - Status: idea

4. **Web client** — a browser client as a second consumer of the orchestrator API.
   - Scope: reuses the same protocol + auth boundary as the TUI; no new data-plane.
   - Depends on: #1 (M1–M6 API) and #3 (auth boundary). Must honour D1–D7.
   - Status: idea

5. **Metrics & observability surface** — dashboards/aggregation over session JSONL.
   - Scope: builds on the pull-from-JSONL metrics (#1 M5); optional live push/tail for
     dashboards. Durable source of truth stays JSONL. Judged non-architectural.
   - Depends on: #1 (M5) for the aggregate endpoint; otherwise independent.
   - Status: idea

## Explicitly not doing (for now)

- **LLM egress proxy / data-plane gateway** — rejected by D2. The container is the sole
  metering point (ACP bypasses any proxy), so a proxy buys nothing and adds a SPOF on
  every token. Orchestrator stays control-plane only.
- **Persisted orchestrator registry** — leaning stateless-rediscover via `docker ps`
  (D4/D7). Revisit only if rediscovery proves insufficient.

---

## How the orchestrator project reshapes this roadmap

Capturing the "does anything change?" analysis so it isn't re-litigated:

- **The refactor is a gate, not a rewrite of everything.** It only blocks work in the
  lifecycle/discovery/addressing layer (→ #3, #4, #5's aggregate endpoint). Work in the
  container/agent-loop/protocol/TUI layer (→ #2) is independent and can proceed in
  parallel.
- **Locked decisions cascade down.** Projects #3–#5 must *honour* D1–D7 (from #1), not
  re-open them. Any project plan for them cites 019's locked decisions rather than
  re-deciding routing/egress/co-location/lifecycle/cred-custody/Ollama-addressing.
- **One shared surface to coordinate:** the WS command set. #1's M3 adds a clean
  termination signal; #2 adds abort/steering. Same vocabulary — decide it once.
- **Web *client* moved to its own project (#4)**, downstream of the orchestrator + auth.
  Distinct from the **orchestrator web *console*** (019 slice M7): the console is a
  read-only, localhost-only face on the orchestrator's own control-plane state (session
  list; a debug console for when the CLI/TUI is broken). It is NOT a session client, so
  it stays inside 019, depends only on M1, and never crosses the M6 auth boundary. The
  web client (#4) drives sessions and does depend on M1–M6 + auth.
