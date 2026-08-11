# Contributing

archie-nexus is a uv workspace with four members: `cli`, `agent`, `shared`, and
`orchestrator`. Python 3.13.

## Setup

```bash
uv sync
```

## Development commands

```bash
uv run pytest              # run the full test suite (tests/)
uv run ruff check .        # lint
uv run ruff format .       # format
```

Ruff is configured in `pyproject.toml` (line length 100, rules `E W F I B C4 N UP`,
`E501` ignored). Prefer scoping `ruff format`/`check` to the files you touched to avoid
sweeping unrelated churn into a change.

## Session events and accounting

Sessions use a flat, tagged `msgspec` canonical event stream defined in
`archie_shared.canonical_events`. See `docs/architecture.md` for the model.

Key rules when working on session/accounting code:

- Body events are **persisted** (JSONL, ULID-ordered) but **not broadcast** on the wire.
  The live display consumes wire events; `/events` replay reads the persisted log.
- `llm_request` is the one canonical event that is both persisted and broadcast — it is
  the single source of truth for token/cost accounting. Historical requests are never
  repriced after a model switch.
- `text_delta`, connection snapshots, and status refreshes are live-only.
- There is no client-side cost derivation from per-model rates. Do not reintroduce
  `cost_per_m_*` fields onto wire events or `MessageMetadata`.

## Subagent scope contract

Subagent attribution maps onto canonical events via `scope`:

- Child `scope` = the launching `task` tool call's `tool_use_id`; the root agent's
  events have `scope=None`.
- Parent reconstruction: `parent_of(S) = tool_call(tool_use_id=S).scope`.
- Request identity is `(scope, turn_iteration, request_id)`; one `llm_request` per child
  provider request.
- Cost aggregation lives in `archie_shared.session.accounting`:
  `scope_direct_costs` (a scope's own requests) and `scope_inclusive_costs` (direct plus
  all descendants, with a cycle guard).

The authoritative version of this contract is the module docstring of
`shared/src/archie_shared/session/accounting.py` and the "Subagent Scope Contract"
section of `plans/029-project-canonical-session-events.md`.

## Conventions

- Match existing code style and patterns; do not add comments unless they clarify
  non-obvious intent.
- Keep tests alongside behaviour changes. Canonical event stream helpers live in
  `tests/conftest.py`.
- Do not commit secrets.
