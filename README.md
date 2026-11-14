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
| `orchestrator/` | Host-side HTTP/WebSocket service, Docker lifecycle, session records, session proxy, log viewer, metrics index and insights pages |
| `agent/` | Container-side Starlette service, agent loop, providers, core tools, plugin SDK, subagents |
| `shared/` | Configuration, models, public commands/events, session-log persistence, credentials, plugin manifests, the transcript model both clients render |
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

`archie start` creates a container only after the image exists and the orchestrator is reachable. Without `--detach`, it opens the [TUI](#terminal-ui) on the new session. Run tests and lint before submitting changes; the authoritative commands are in [Contributing](CONTRIBUTING.md).

```bash
archie ls
archie attach <session-id-or-prefix>       # prefix matching
archie shell <session-id-or-prefix>        # prefix matching
archie stop <exact-session-id>             # exact matching
archie start --detach <workspace>
archie start <profile>/<workspace>
archie attach <profile>/<session-id-or-prefix>
```

Usage insights come from the local metrics index and work without the orchestrator running. `archie stats` prints the overview; the subcommands are `sessions`, `session <id>`, `models`, `tools`, `subagents` and `errors`. Every view takes `--since` (`7d`, `all` or `YYYY-MM-DD`; default `30d`), `--until`, `--workspace`, `--model`, `--scope` (`all`, `root`, `subagent`) and `--json`. The same views, with charts and filters, are at `http://127.0.0.1:7600/insights` while `archie serve` runs.

```bash
archie stats --since 7d
archie stats sessions --sort cost --limit 20
archie stats session <session-id> --json
```

Build the image again after changing Dockerfile or dependency layers. Source for `agent`, `shared`, `persona`, and `plugins` is mounted for development.

## Terminal UI

`archie start` and `archie attach` open the same window on one orchestrator profile: a sidebar listing every live session of that orchestrator, grouped by workspace, beside the pane of the session on screen. `archie start` shows the session it created; `archie attach <id>` shows that session (a prefix that matches several asks for one by number), and `archie attach` with no ID shows the newest live session, or an empty pane when none runs. Each sidebar row shows the session's state, name, age, and the repo and branch it works in.

- Switch sessions by clicking a row, with alt+↑/↓, or with alt+1…9 (the row's place in the sidebar). ctrl+f filters the sidebar by name, ID, workspace, repo or branch; tab and shift+tab move between the filter, the list, the transcript and the input.
- The pane shows the transcript, the activity line, any pending question, the input and a status bar. Clicking a tool or subagent row opens its view in the pane, in the transcript's place; ctrl+x stops the subagent shown in an open subagent view. esc does the first of these that applies: kill a running `!` command, close the open view, dismiss the question, interrupt the turn; a second esc within half a second clears the input.
- ctrl+n opens the new-session dialog: pick a workspace and the Archie or Kiro backend, and the window switches to the new session. alt+m changes the model. ctrl+p opens the palette for the session on screen: New session, Rename session, Change model, Compact context (Archie sessions only), Stop session (asks first; the log is kept), Open web interface, Keyboard shortcuts and Quit. f1 lists every key.
- Notices in the sidebar report other sessions' replies, questions, failed turns and ends; clicking one shows that session. If the orchestrator restarts, the window reconnects on its own.
- `!command` runs `command` in the session's container with `docker exec`, without the agent. It only works on a local orchestrator (the default profile with no `ARCHIE_HOST`); on a remote profile the pane says so and runs nothing.
- ctrl+c copies the selection, or else the focused block; ctrl+g writes the message in `$EDITOR` (default `nano`) and sends it when you save.
- ctrl+q quits after asking. Quitting closes the window only: every session keeps running, and `archie attach` opens it again. Stop a session from the palette or with `archie stop`.

## Configuration and data

The default user-data directory is `~/.nexus`, overridable with `ARCHIE_HOME_DIR`. Configuration is `~/.nexus/config.yaml`; an absent file uses defaults. Important settings are `global.model`, `global.region`, `global.workspace_root`, `agent.subagents.max_concurrent`, `agent.turn_cost_limit` (per-turn USD budget, default `50.0`), named `orchestrator.profiles`, per-integration `plugins.<id>` sections, and top-level `mounts` for host bind mounts. See [Configuration](docs/config.md) for the complete schema and mount examples.

Session event logs are stored under `~/.nexus/sessions/<session-id>.jsonl`; `~/.nexus/archie.db` holds the orchestrator's session records plus a metadata index built from those logs (rebuildable at any time); an old `~/.nexus/metrics.db` is no longer used and can be deleted. Credentials default to `~/.nexus/credentials.yaml`, and brain data defaults to `~/.nexus/brain`. `ARCHIE_BRAIN_DIR` can override the brain location, while `ARCHIE_PERSONA_DIR` overrides the persona directory and `ARCHIE_PLUGINS_DIR` the plugins directory. See [Architecture](docs/architecture.md#configuration-and-ownership) before changing persisted or wire formats.

For multiple orchestrators, define named profiles under `orchestrator.profiles` and use `profile/value` arguments. `ARCHIE_HOST=host[:port]` overrides profile selection for CLI commands; the default port is 7600. The `serve` command binds to the configured default profile, so binding it to a non-loopback host exposes the orchestrator to that network.

## Scope

This repository is the nexus implementation only. The parent workspace contains other Archie projects; their instructions and runtime assumptions are separate. Start with the documentation inside the repository you are changing.
