# archie-nexus

archie-nexus is Archie’s current host/container implementation: a Python 3.13 workspace that runs an agent in a Docker container and exposes it through a host-side orchestrator. The `archie` CLI builds images, starts and manages sessions, and attaches a Textual terminal UI.

## Start here

- [Architecture](docs/architecture.md) — components, lifecycle, data flows, HTTP/WebSocket routes, event layers, logs, accounting, and security boundaries.
- [Configuration](docs/config.md) — application settings and host bind mounts, including wildcard Git and SSH mounts.
- [Authentication and credentials](docs/auth-credentials.md) — provider configuration, credential storage, CLI workflows, OAuth, and agent access.
- [Plugins](docs/plugins.md) — integration layout, `plugin.yaml` reference, plugin SDK, and testing.
- [System prompt](docs/system-prompt.md) — prompt sections and order, how the prompt is built, what varies for subagents, and how to add a tool.
- [Contributing](CONTRIBUTING.md) — development workflow, checks, and invariants for changing the system.
- [Agent-session guidance](AGENTS.md) — short context and reading order for coding agents.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `cli/` | `archie` command, Textual TUI, orchestrator client, auth commands |
| `orchestrator/` | Host-side HTTP/WebSocket service, Docker lifecycle, session proxy, log viewer, metrics index and insights pages |
| `agent/` | Container-side Starlette service, agent loop, providers, core tools, plugin SDK, subagents |
| `shared/` | Configuration, models, public commands/events, session-log persistence, credentials, plugin manifests |
| `plugins/` | One directory per integration: `plugin.yaml`, code, skills, tests ([Plugins](docs/plugins.md)) |
| `persona/` | Repository-tracked prompts, agents, and procedure skills mounted into sessions |
| `container/` | Files copied into the agent image, such as the `gh` wrapper |
| `tests/` | Cross-package and component tests (plugin tests live in `plugins/<id>/tests/`) |
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

Usage insights come from the local metrics index and work without the orchestrator running. `archie stats` prints the overview; the subcommands are `sessions`, `session <id>`, `models`, `tools`, `subagents` and `errors`. Every view takes `--since` (`7d`, `all` or `YYYY-MM-DD`; default `30d`), `--until`, `--workspace`, `--model`, `--scope` (`all`, `root`, `subagent`) and `--json`. The same views, with charts and filters, are at `http://127.0.0.1:7600/ui/insights` while `archie serve` runs.

```bash
archie stats --since 7d
archie stats sessions --sort cost --limit 20
archie stats session <session-id> --json
```

Build the image again after changing Dockerfile or dependency layers. Source for `agent`, `shared`, `persona`, and `plugins` is mounted for development.

## Configuration and data

The default user-data directory is `~/.nexus`, overridable with `ARCHIE_HOME_DIR`. Configuration is `~/.nexus/config.yaml`; an absent file uses defaults. Important settings are `global.model`, `global.region`, `global.workspace_root`, `agent.subagents.max_concurrent`, `agent.turn_cost_limit` (per-turn USD budget, default `50.0`), named `orchestrator.profiles`, per-integration `plugins.<id>` sections, and top-level `mounts` for host bind mounts. See [Configuration](docs/config.md) for the complete schema and mount examples.

Session event logs are stored under `~/.nexus/sessions/<session-id>.jsonl`; `~/.nexus/metrics.db` is a metadata index built from those logs (rebuildable at any time). Credentials default to `~/.nexus/credentials.yaml`, and brain data defaults to `~/.nexus/brain`. `ARCHIE_BRAIN_DIR` can override the brain location, while `ARCHIE_PERSONA_DIR` overrides the persona directory and `ARCHIE_PLUGINS_DIR` the plugins directory. See [Architecture](docs/architecture.md#configuration-and-ownership) before changing persisted or wire formats.

For multiple orchestrators, define named profiles under `orchestrator.profiles` and use `profile/value` arguments. `ARCHIE_HOST=host[:port]` overrides profile selection for CLI commands; the default port is 7600. The `serve` command binds to the configured default profile, so binding it to a non-loopback host exposes the orchestrator to that network.

## Scope

This repository is the nexus implementation only. The parent workspace contains other Archie projects; their instructions and runtime assumptions are separate. Start with the documentation inside the repository you are changing.
