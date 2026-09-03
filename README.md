# archie-nexus

archie-nexus is Archie’s current host/container implementation: a Python 3.13 workspace that runs an agent in a Docker container and exposes it through a host-side orchestrator. The `archie` CLI builds images, starts and manages sessions, and attaches a Textual terminal UI.

## Start here

- [Architecture](docs/architecture.md) — components, lifecycle, data flows, HTTP/WebSocket routes, event layers, logs, accounting, and security boundaries.
- [Contributing](CONTRIBUTING.md) — development workflow, checks, and invariants for changing the system.
- [Agent-session guidance](AGENTS.md) — short context and reading order for coding agents.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `cli/` | `archie` command, Textual TUI, orchestrator client, auth commands |
| `orchestrator/` | Host-side HTTP/WebSocket service, Docker lifecycle, session proxy, metrics |
| `agent/` | Container-side Starlette service, agent loop, providers, tools, subagents |
| `shared/` | Configuration, models, public commands/events, session-log persistence, credentials |
| `persona/` | Repository-tracked prompts, agents, and skills mounted into sessions |
| `tests/` | Cross-package and component tests |
| `plans/` | Design and implementation records; consult relevant plans when changing a contract |

## Quick start

Requirements: Python 3.13, `uv`, a running Docker daemon, and credentials for the selected model provider. The workspace you start must be an existing directory below `global.workspace_root` (default `~/dev`).

```bash
uv sync
archie build
archie serve                  # keep this process running
archie start                  # current project, when it is below workspace_root
```

`archie start` creates a container only after the image exists and the orchestrator is reachable. Without `--detach`, it attaches the TUI. Run tests and lint before submitting changes; the authoritative commands are in [Contributing](CONTRIBUTING.md).

```bash
archie ls
archie attach <session-id-or-prefix>       # prefix matching
archie shell <session-id-or-prefix>        # prefix matching
archie stop <exact-session-id>             # exact matching
archie start --detach <workspace>
archie start <profile>/<workspace>
archie attach <profile>/<session-id-or-prefix>
```

Build the image again after changing Dockerfile or dependency layers. Source for `agent`, `shared`, and `persona` is mounted for development.

## Configuration and data

The default user-data directory is `~/.nexus`, overridable with `ARCHIE_HOME_DIR`. Configuration is `~/.nexus/config.yaml`; an absent file uses defaults. Important settings are `global.model`, `global.region`, `global.workspace_root`, `agent.subagents.max_concurrent`, and named `orchestrator.profiles`.

Session event logs are stored under `~/.nexus/sessions/<session-id>.jsonl`; aggregate request metrics are stored in `~/.nexus/metrics.db`. Credentials default to `~/.nexus/credentials.yaml`, and brain data defaults to `~/.nexus/brain`. `ARCHIE_BRAIN_DIR` can override the brain location, while `ARCHIE_PERSONA_DIR` overrides the persona directory. See [Architecture](docs/architecture.md#configuration-and-ownership) before changing persisted or wire formats.

For multiple orchestrators, define named profiles under `orchestrator.profiles` and use `profile/value` arguments. `ARCHIE_HOST=host[:port]` overrides profile selection for CLI commands; the default port is 7600. The `serve` command binds to the configured default profile, so binding it to a non-loopback host exposes the orchestrator to that network.

## Scope

This repository is the nexus implementation only. The parent workspace contains other Archie projects; their instructions and runtime assumptions are separate. Start with the documentation inside the repository you are changing.
