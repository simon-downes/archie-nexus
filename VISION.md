# Archie Nexus — Vision

A personal AI platform split into multiple packages: client interfaces (CLI, web)
that manage and connect to container sessions, and a headless agent server that runs
inside them. Each session is a container. Clients attach and detach freely.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Clients (installed on machine)                          │
│                                                          │
│  archie-cli: archie start | ls | shell | build | auth    │
│  archie-web: browser UI (future)                         │
│                                                          │
│  Responsibilities:                                       │
│  - Container lifecycle (start, stop, list)               │
│  - Image building                                        │
│  - Credential management (OAuth refresh, token storage)  │
│  - Session discovery (list, attach, detach)              │
│  - Log aggregation + memory extraction                   │
│  - Brain git operations (clone, pull, commit)            │
│                                                          │
│  Does NOT: run agent loops, call LLMs, execute tools,    │
│  hold conversation state, assemble prompts               │
└─────────────────────────┬────────────────────────────────┘
                          │
                          │  HTTP/WebSocket (mapped port)
                          │  docker exec (shell access)
                          │
┌─────────────────────────▼────────────────────────────────┐
│  archie-agent (runs inside container)                    │
│                                                          │
│  Headless server on :8080                                │
│                                                          │
│  Responsibilities:                                       │
│  - Agent loop                                            │
│  - LLM clients (Bedrock, Ollama, Cloudflare AI Gateway)  │
│  - Tool execution (all tools are container-side)         │
│  - Prompt assembly + skills                              │
│  - Session state + JSONL log persistence                 │
│  - Event streaming to connected clients                  │
│  - ACP sub-processes (kiro-cli / maki)                   │
│                                                          │
│  Mounts:                                                 │
│  - /workspace          (project dir, rw)                 │
│  - /opt/archie/agent   (agent source, ro — dev mount)    │
│  - /archie/brain       (brain, rw)                       │
│  - <ARCHIE_HOME_DIR>/sessions  (session logs, rw)           │
│  - /archie/skills      (skills, ro)                      │
│  - /archie/credentials (tokens + keys, ro)               │
│  - ~/.ssh              (git auth, ro)                     │
│  - ~/.gitconfig        (git config, ro)                  │
└──────────────────────────────────────────────────────────┘
```

---

## Principles

1. **Shared protocol, separate concerns.** Common types (wire protocol events,
   message types, serialization) live in `archie-shared` and are imported by both
   client and agent packages. Business logic remains separated — clients handle UI
   and container lifecycle, the agent handles LLM orchestration and tool execution.

2. **Session = container.** Each conversation is a container instance. Named sessions
   are named containers. Concurrent sessions are concurrent containers. Clients are
   disposable; sessions persist.

3. **The container is the agent.** The agent loop, LLM client, and tools all run
   inside the container as a headless server. No TUI in the container — clients
   provide the interface.

4. **Clients attach and detach.** Any client can connect to any running session, see
   the conversation history, and continue interacting. Closing a client does not stop
   the session. Multiple clients can observe the same session simultaneously.

5. **Unified interface, multiple backends.** The agent loop supports multiple LLM
   backends (Bedrock, Ollama, Cloudflare, ACP) through a common client protocol.
   Switching backends is a config change, not an architecture change.

6. **Tools as async functions.** The primary tool interface is `exec` — the model
   writes Python defining `async def main()` and calls tool functions directly.
   No JSON-schema marshalling for the primary workflow.

7. **Files over services.** Brain is a git repo on disk. Session logs are JSONL files.
   Credentials are files in a mounted directory. No databases, no running services
   required beyond Docker and optionally Ollama.

8. **Host handles the slow/sensitive operations.** OAuth token refresh, brain git
   commits, image builds, credential rotation — anything that needs long-lived state
   or elevated access lives on the host.

---

## Client Package: archie-cli

Installed directly on the machine. Provides the `archie` CLI command.

### Container Lifecycle

```bash
archie build                  # build the agent image
archie start                  # start a new session (background, project-scoped)
archie start --name fix-auth  # start/resume named session
archie ls                     # list running sessions with ports
archie shell [session]        # interactive bash in a session (prefix match)
archie stop [session]         # stop a session
archie attach [session]       # connect TUI to a session (future)
```

Starting a session:
1. Detect project (cwd → project root)
2. Generate session ID (date-project-random)
3. Build mount list (agent source, workspace, brain, credentials, dotfiles)
4. Set env vars (model, region, session name, Ollama URL, etc.)
5. `docker run -d --rm -p 127.0.0.1:0:8080` with the archie image
6. Container starts agent server; host reports session ID and port

Sessions run in the background. Clients connect via HTTP/WebSocket to the mapped
port. `docker exec -it` provides shell access without going through the agent.

### Credential Management

```bash
archie auth login google      # OAuth flow, stores refresh token
archie auth login slack       # OAuth flow
archie auth status            # show active credentials
```

Credentials are stored at `~/.archie/credentials/` as JSON files per provider.
The host handles token refresh (cron or on-demand before session start). Containers
mount the credentials directory read-only and use the tokens directly.

### Log Aggregation + Memory

Session logs are written by the container to the mounted sessions directory. The host
reads them for:
- Memory extraction (process new turns → brain updates)
- Cross-session summaries
- Cost tracking and reporting

This can run as a post-session hook or a periodic background process.

### Brain Git Operations

The brain lives at `~/.archie/brain/` as one or more git repos. The host handles
repo-level operations (clone, pull, push, commit). Containers mount the brain rw and
write files directly — the host commits and pushes on a schedule or on session end.

---

## Client Package: archie-web (future)

A web application running on the host providing browser-based access to sessions.

- Dashboard showing all running sessions
- Connect to any session (conversation view + interaction)
- Terminal widget (xterm.js) via Docker exec API relay
- Multiple sessions visible simultaneously
- Remote access (with auth) to sessions on other hosts

Communication with containers is identical to archie-cli — HTTP/WebSocket to the
mapped port. Terminal access uses Docker's exec API with a WebSocket bridge.

---

## Agent Package: archie-agent

Runs inside Docker containers as a headless HTTP/WebSocket server.

### Server Interface

The agent exposes an HTTP/WS API on port 8080:

- `GET /status` — health check, session metadata
- `POST /message` — submit a user message, triggers agent turn
- `WS /stream` — bidirectional: receive events (text, tool calls, results),
  send messages and interrupts
- `GET /history` — retrieve conversation history (for client catch-up)

Clients connect via WebSocket to receive streaming events. Multiple clients can
connect simultaneously — all receive the same event stream (fan-out).

### Agent Loop

The core conversation loop:
1. Receive user message (from HTTP/WS)
2. Build messages (system prompt + conversation history)
3. Send to LLM (streaming)
4. Process response (text + tool calls)
5. Execute tools (via `exec` or discrete tools)
6. Stream events to all connected clients
7. Loop until no more tool calls
8. Persist turn to session log

### LLM Backends

A single `LLMClient` protocol satisfied by multiple implementations:

- **Bedrock** — Claude models via AWS. Supports streaming, prompt caching. Needs AWS
  credentials (mounted from host).
- **Ollama** — local models. Container reaches Ollama on the host via
  `host.docker.internal:11434` (Docker adds `--add-host=host.docker.internal:host-gateway`).
- **Cloudflare AI Gateway** — serverless models via OpenAI-compatible API.
- **ACP** — delegate to kiro-cli or maki running as sub-processes inside the same
  container. Used for tasks that benefit from those tools' specific capabilities.

Backend selection is per-session (configured at start) or per-request (model routing
for subagents — e.g. cheap model for extraction, strong model for reasoning).

### Tool Execution — The `exec` Contract

The primary tool is `exec`. The model submits a Python module:

```python
async def main():
    hits = await grep(pattern="TODO", include="*.py")
    files = list({h["path"] for h in hits})
    contents = await asyncio.gather(*(read(path=f) for f in files[:5]))
    return {"files": files, "contents": contents}
```

Contract:
- Source MUST define `async def main()` (no required arguments)
- Return value is the structured result (JSON-serialisable → pass-through; otherwise repr)
- `print()` output captured as stdout
- `asyncio` available for concurrency
- Tool functions available as async callables in the namespace
- Errors raise typed exceptions (caught by runner, surfaced in result)

The runner captures: return value, stdout, stderr, exceptions with traceback, timing,
and an audit log of which tool functions were called.

### Available Tools

Exposed as async functions inside `exec`:

**Filesystem:** `read`, `write`, `edit`, `grep`, `glob`
**Execution:** `shell`
**Web:** `web_fetch`, `web_search`
**Code intelligence:** `code` (tree-sitter structural analysis)
**Knowledge:** `brain_search`, `brain_read`, `brain_write`
**Integrations:** `notion`, `linear`, `jira`, `slack`, `google`

Each returns idiomatic Python types (str, list[dict], dict, None) and raises typed
exceptions on error (`PathValidationError`, `EditError`, `ConnectionError`, etc.).

A small set of discrete tools remain outside `exec` for operations that don't benefit
from code composition: `skill` (load skill into prompt), `recall` (search memory),
`retrieve_artifact` (recover truncated results).

### Prompt Assembly

System prompt is built from:
1. Soul (identity, personality, principles)
2. Guidance (tool strategy, conventions — model-specific where needed)
3. Environment (project, branch, OS)
4. Skills catalog (available + loaded)
5. Project context (AGENTS.md)

Skills are loaded on-demand via the `skill` tool and injected into the system prompt
for the remainder of the session.

### ACP — Sub-agents

For tasks that benefit from kiro-cli or maki's capabilities:
- The agent spawns them as sub-processes within the container
- Communication via ACP (Agent Communication Protocol) over stdio
- The parent agent sends a task, waits for completion, receives the result
- Sub-agent session logs are captured and consolidated into the parent session

ACP is also the mechanism for subagent dispatch — the agent can spawn lightweight
sub-agents (code reviewer, plan reviewer, QA runner) as separate processes with
restricted tool sets. From the client's perspective, sub-agent work appears as
tool-use events in the stream.

### Session Persistence

Each turn is appended to a JSONL file at `<ARCHIE_HOME_DIR>/sessions/{session-id}.jsonl`.

Entry format:
```json
{
  "id": "ulid",
  "when": "iso8601",
  "user": "user prompt text",
  "assistant": "response text",
  "tools": [{"name": "exec", "source": "...", "result": "..."}],
  "metadata": {
    "model": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "backend": "bedrock",
    "input_tokens": 1234,
    "output_tokens": 567,
    "cost": 0.004
  }
}
```

The session file is the canonical record. Clients connecting to a running session
receive history from the JSONL log (catch-up replay). The host reads logs for memory
extraction and aggregation.

---

## Communication

### Client → Container (Agent API)

- **HTTP/WebSocket** to mapped port (127.0.0.1:{ephemeral} → container:8080)
- Port discovered via `docker port archie-{session_id} 8080`
- Messages, streaming events, history retrieval all via this channel

### Client → Container (Shell)

- **docker exec -it** for interactive bash (CLI)
- **Docker exec API + WebSocket relay** for browser terminals (web client)
- Independent of the agent API — direct process execution in container

### Container → Ollama

- HTTP to `host.docker.internal:11434`
- Docker run includes `--add-host=host.docker.internal:host-gateway`

### Container → Cloud (Bedrock / Cloudflare)

- Direct HTTPS from inside the container
- AWS credentials mounted from host

---

## Container Paths

All paths inside the container use `/opt/archie/` or `/archie/`:

| Path | Purpose | Mount type |
|------|---------|------------|
| `/opt/archie/agent/` | Agent source code | ro (dev mount from host) |
| `/opt/archie/venv/` | Python venv (deps cached at build) | built into image |
| `/opt/archie/entrypoint.sh` | Startup script | built into image |
| `/workspace` | Project directory | rw (from host CWD) |
| `/archie/brain` | Brain git repo | rw |
| `<ARCHIE_HOME_DIR>/sessions` | Session JSONL logs | rw |
| `/archie/skills` | Skills directory | ro |
| `/archie/credentials` | OAuth tokens + keys | ro |

---

## Development Workflow

The agent source (`agent/`) is mounted into containers at runtime rather than baked
into the image. This means:

- **No rebuild needed** for Python code changes — just start a new container
- **Rebuild only when** dependencies change or system tools are modified
- The Dockerfile pre-installs dependencies (cached layer); `entrypoint.sh` does a
  fast `uv sync` at startup to link the mounted source

---

## Evolution Path

This architecture is implemented incrementally. Each step delivers usable value:

1. **Skeleton** — workspace structure, Dockerfile, container lifecycle commands
   (build, start, ls, shell), agent server with health endpoint.

2. **Agent loop** — LLM client (Bedrock), basic agent loop, message API, event
   streaming over WebSocket.

3. **Tools** — exec runner, filesystem tools, shell tool inside container.

4. **CLI client** — TUI (Textual) that connects to agent WS, displays conversation,
   sends messages.

5. **Session management** — named sessions, resume from JSONL, history replay on
   client connect.

6. **Multi-backend** — Ollama via host.docker.internal, Cloudflare AI Gateway.

7. **Credential injection** — host manages OAuth flows, mounts credential store.

8. **Integration tools** — port agent-kit integrations (Notion, Linear, Slack, etc.)
   into agent-side tool functions.

9. **ACP** — sub-process management for kiro-cli, maki, and lightweight subagents.

10. **Memory loop** — host-side extraction from session logs, brain updates.

11. **Web client** — browser UI with session dashboard, xterm.js terminals, multi-
    session view.

12. **Remote sessions** — run containers on remote hosts, connect from anywhere
    (with auth).

Each step has its own plan. This document is the reference for what the system looks
like when they're all complete.
