# Agent guidance for archie-nexus

This file is the short session-start guide. Use the linked documents for detail rather than copying their contracts here.

## First read

1. [README.md](README.md) for repository layout, setup, and commands.
2. [docs/architecture.md](docs/architecture.md) before changing components, routes, events, logs, session lifecycle, or accounting.
3. [CONTRIBUTING.md](CONTRIBUTING.md) before changing code; it lists checks and non-negotiable invariants.
4. Relevant package `pyproject.toml`, tests, and plans. Tests are often the most precise examples of protocol behavior.

## Repository orientation

- `cli/` is the host-facing command and TUI client.
- `orchestrator/` is the host-side control plane and Docker/session proxy.
- `agent/` runs inside each session container and owns the agent loop and tools.
- `shared/` defines contracts used across those boundaries. Treat changes here as API/schema changes.
- `persona/` is runtime prompt/agent/skill content, not Python package code.

## Working rules

- Keep changes scoped to the requested behavior; preserve existing patterns.
- Run focused tests while iterating, then use the complete checks in [CONTRIBUTING.md](CONTRIBUTING.md) for a completed change.
- Update architecture documentation when a route, mount, event, persisted format, accounting rule, or component boundary changes.
- Do not silently change canonical event or WebSocket wire schemas. Add/update tests and document compatibility implications.
- Never commit credentials, tokens, generated session data, `.venv`, caches, or local Docker artifacts.

## Environment note

Nexus may be developed from a containerized agent runtime, but those mount and runtime details are environment-specific. They belong in the runtime/harness instructions, not in this repository-level guide. This file describes the repository itself and does not assume a particular agent execution environment.
