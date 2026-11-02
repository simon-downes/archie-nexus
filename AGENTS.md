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
- `agent/` runs inside each session container and owns the agent loop, core tools, and the plugin SDK.
- `shared/` defines contracts used across those boundaries. Treat changes here as API/schema changes.
- `plugins/` holds one directory per integration (manifest, code, skills, tests); see [docs/plugins.md](docs/plugins.md).
- `persona/` is runtime prompt/agent/procedure-skill content, not Python package code.

## Working rules

- Keep changes scoped to the requested behavior; preserve existing patterns.
- Run focused tests while iterating, then use the complete checks in [CONTRIBUTING.md](CONTRIBUTING.md) for a completed change.
- Update architecture documentation when a route, mount, event, persisted format, accounting rule, or component boundary changes.
- Do not silently change canonical event or WebSocket wire schemas. Add/update tests and document compatibility implications.
- Never commit credentials, tokens, generated session data, `.venv`, caches, or local Docker artifacts.

## Environment note

Run syncs and checks through `scripts/dev` (`scripts/dev sync`, `scripts/dev test`, `scripts/dev ruff check .`) instead of calling `uv sync` or `uv run` directly. Inside an Archie session container, plain `uv` tries to build `tree-sitter-vb-dotnet` from source and fails without a C compiler; the script installs the image's prebuilt wheel instead. It also points `ARCHIE_PERSONA_DIR` and `ARCHIE_PLUGINS_DIR` at this worktree rather than the session's `/opt/archie` copies.

Inside a session, `/opt/archie/venv` is the running agent's environment. Do not sync into it or use it for worktree tests; the worktree uses `.venv-archie`.
