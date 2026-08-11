# Architecture

## Canonical session events

Nexus sessions use a flat, tagged `msgspec` event stream defined in
`archie_shared.canonical_events`. Persisted events are JSONL records with unique
ULID IDs; JSONL append order is authoritative. Body events are persisted but not
broadcast on the wire; the live display consumes wire events. The one exception is
`llm_request`, which is both persisted and broadcast so clients and orchestrator
metrics can accumulate cost/token totals.

Per-message session-log metadata (`MessageMetadata`) carries only model and
interruption state. All token and cost accounting lives in `llm_request` events —
there is no client-side cost derivation from per-model rates.

Each provider request produces exactly one `llm_request` event containing the
model key, request status, actual token categories, context token count, timing,
and immutable cost. Clients and orchestrator metrics consume this record directly;
historical requests are never repriced after model switches.

`text_delta`, connection snapshots, and status refreshes are live-only. Replay
uses persisted assistant messages and request events. Subagent attribution uses
the launching tool's `tool_use_id` as `scope`; direct and inclusive scope costs
are derived from atomic request events.

The canonical event model is the accounting and replay foundation for plan 028.
Plan 028 must consume this contract before subagent implementation begins.
