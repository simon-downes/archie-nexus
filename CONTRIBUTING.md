# Contributing to archie-nexus

See [README.md](README.md) for setup and the repository map. See [docs/architecture.md](docs/architecture.md) for the system contracts behind the guidance below.

## Development setup

archie-nexus is a `uv` workspace containing `cli`, `agent`, `shared`, and `orchestrator`. It requires Python 3.13.

```bash
uv sync
```

The CLI package installs the `archie` command. The agent package installs `archie-agent`; the orchestrator is normally started through `archie serve`.

## Checks

These are the authoritative completion checks:

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

Prefer scoping Ruff commands to files you touched to avoid unrelated churn. Ruff is configured in `pyproject.toml` with line length 100 and rules `E W F I B C4 N UP` (`E501` is ignored).

## Change guidance

- Put behavior tests in `tests/` and update tests with behavior changes.
- Treat `shared/` changes as cross-package contract changes. Check both agent and CLI/orchestrator consumers.
- Keep host-only concerns in `cli`/`orchestrator`; keep container-side execution in `agent`.
- Keep reusable schemas, serialization, configuration, and accounting in `shared` rather than duplicating them in a presentation layer.
- Update [docs/architecture.md](docs/architecture.md) when changing component boundaries, Docker mounts, routes, public commands/events, persistence, metrics, or accounting.

## Session and accounting invariants

The authoritative detailed contract is in [docs/architecture.md](docs/architecture.md), `shared/src/archie_shared/events.py`, `shared/src/archie_shared/commands.py`, and `shared/src/archie_shared/session/accounting.py`.

- The persisted session stream is canonical JSONL in append order. Event machinery normally generates ULID-like IDs; persistence requires non-empty IDs, rejects conflicting duplicate IDs, and treats identical retries as idempotent.
- The session coordinator is the sole public-event sink: persisted events are appended before broadcast, while live-only events are queued without persistence.
- The persisted and live-only public events are one tagged contract. Do not assume every live-only frame is persisted or every persisted event is live-delivered.
- `llm_request` is the source of truth for token and cost totals. Historical requests retain their recorded immutable cost; do not reprice them after a model change.
- A child agent’s `scope` is the launching task tool call’s `tool_use_id`; root events use `scope=None`.
- Request identity is `(scope, subagent_index, turn, iteration, request_id)`. Keep child direct/inclusive aggregation cycle-safe.
- Legacy `MessageMetadata` exists only for stopped-session migration and must not become an authoritative or recomputed accounting source. Do not add client-side rate calculations or cost-per-token fields to public events.

## Review checklist

Before submitting a change:

- tests cover the changed behavior and relevant failure paths;
- schema/protocol compatibility has been considered;
- docs and examples match the current code;
- secrets and generated artifacts are absent;
- `uv run pytest` and `uv run ruff check .` pass (or failures are explained).
