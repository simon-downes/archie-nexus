# Plan 002: Agent Loop + TUI + WebSocket Communication

## Objective

Implement end-to-end text conversation between a Textual TUI client and a containerised
agent server, communicating over WebSocket, with Bedrock as the LLM backend. No tool
execution yet (plan 003).

## Context

Plan 001 delivered the container lifecycle (build/start/ls/shell). This plan adds the
actual intelligence — an agent loop that calls Bedrock, streams responses over WebSocket
to connected TUI clients, and supports reconnection with history catch-up.

The nextgen project has working implementations of the Bedrock client, agent loop, and
TUI that are being ported/adapted. The key architectural difference is the network
boundary — nextgen is monolithic (single process), nexus splits agent and UI across a
WebSocket.

---

## Requirements

### Functional

1. MUST create an `archie-shared` workspace package containing wire protocol event types
   and serialization, importable by both CLI and agent packages
2. MUST implement a Bedrock streaming client (ported from nextgen) that yields typed
   stream events
3. MUST implement a model catalog with pricing, context limits, and region info (ported
   from nextgen)
4. MUST implement a config system reading `~/.archie/nexus.yaml` with model, region, and
   project_root settings
   - AC: Config file is mounted read-only into the container at `/archie/config/nexus.yaml`
   - AC: Both CLI and agent read the same config file (via archie-shared)
5. MUST implement an agent loop that receives user messages, builds conversation context,
   calls Bedrock, and emits events
   - AC: Agent loop runs Bedrock streaming synchronously in a thread
   - AC: Events are broadcast to all connected WebSocket clients
6. MUST implement WebSocket endpoint (`WS /stream`) for bidirectional communication
   - AC: Server→client: streams typed JSON events (text_delta, usage_updated,
     turn_complete, turn_error)
   - AC: Client→server: accepts message and interrupt commands
   - AC: Multiple clients can connect simultaneously and all receive the same events
7. MUST implement `GET /history` endpoint returning conversation history for client
   catch-up on connect
8. MUST implement a Textual TUI (ported from nextgen) with conversation display, streaming
   text rendering, message input, and status bar
   - AC: TUI connects to agent WebSocket on startup
   - AC: On connect, fetches history and replays it to populate conversation
   - AC: Displays streaming text as it arrives
   - AC: Shows token usage and cost in status bar
9. MUST add `archie attach [session]` CLI command that launches the TUI connected to a
   running session's port
   - AC: Supports prefix matching on session ID (same as `archie shell`)
10. MUST support user interrupt (Esc) that signals the agent to cancel the current turn
    - AC: Interrupt sent via WebSocket, agent stops generation, streams TurnInterrupted event

### Non-Functional

11. SHOULD handle WebSocket disconnection gracefully — agent continues running, client
    can reconnect
12. SHOULD handle Bedrock throttling with exponential backoff retry (port from nextgen)
13. MUST mount `~/.archie/nexus.yaml` read-only into containers (CLI `start` command updated)
14. MUST mount `~/.aws/` read-only into containers for Bedrock credential access

---

## Design

### Technology Choices

- **Shared package:** `archie-shared` — third uv workspace member at `shared/`, import
  name `archie_shared`. Contains wire protocol events, content block types, model catalog,
  and config loading.
- **Agent WebSocket server:** Starlette (already a dependency) with WebSocket routes.
- **TUI framework:** Textual (added to CLI deps). Ported from nextgen.
- **TUI WebSocket client:** `websockets` library (lightweight, async-native).
- **Bedrock client:** boto3 (synchronous generator, run via `asyncio.to_thread`).
- **Config format:** YAML via pyyaml.

### Structure

```
archie-nexus/
├── shared/
│   ├── pyproject.toml
│   └── src/archie_shared/
│       ├── __init__.py
│       ├── config.py        # Config loading (nexus.yaml)
│       ├── events.py        # Wire protocol events + serialization
│       ├── models.py        # Model catalog, pricing, ModelInfo
│       └── types.py         # ContentBlock types (TextBlock, etc.)
├── agent/
│   ├── pyproject.toml       # adds: boto3, archie-shared
│   └── src/archie_agent/
│       ├── app.py           # Starlette app + WebSocket routes
│       ├── agent.py         # AgentLoop (text-only, no tools)
│       ├── llm/
│       │   ├── __init__.py  # LLMClient protocol, re-exports
│       │   └── bedrock.py   # BedrockClient (ported from nextgen)
│       └── session.py       # Session state + JSONL persistence
├── cli/
│   ├── pyproject.toml       # adds: textual, websockets, archie-shared
│   └── src/archie_cli/
│       ├── cli.py           # adds: attach command
│       ├── tui/
│       │   ├── __init__.py
│       │   ├── app.py       # ArchieApp (connects via WS)
│       │   ├── conversation.py
│       │   ├── input.py
│       │   ├── status.py
│       │   ├── theme.py
│       │   ├── throbber.py
│       │   └── archie.tcss
│       └── ws_client.py     # WebSocket client wrapper
```

### Wire Protocol

Server → Client events (JSON over WebSocket). Each event carries a `turn_index`
(monotonic int, incremented per user message) so clients can reconcile buffered events
against fetched history on connect:
```json
{"type": "text_delta", "turn_index": 3, "data": {"text": "chunk..."}}
{"type": "usage_updated", "turn_index": 3, "data": {"input_tokens": 1234, "output_tokens": 567, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost": 0.004}}
{"type": "turn_complete", "turn_index": 3, "data": {"stop_reason": "end_turn"}}
{"type": "turn_interrupted", "turn_index": 3, "data": {}}
{"type": "turn_error", "turn_index": 3, "data": {"message": "..."}}
```

Client → Server messages:
```json
{"type": "message", "data": {"content": "user text..."}}
{"type": "interrupt", "data": {}}
```

### History Endpoint

`GET /history` returns JSON array of turns using content-block format (forward-compatible
with tool blocks in plan 003). Each turn includes its `turn_index` for client-side
reconciliation with buffered WS events. `turn_index` is per-exchange: a user message and
its corresponding assistant response share the same index (incremented once per user
message):
```json
[
  {"turn_index": 1, "role": "user", "content": [{"type": "text", "text": "hello"}]},
  {"turn_index": 1, "role": "assistant", "content": [{"type": "text", "text": "Hi! How can I help?"}]},
  {"turn_index": 2, "role": "user", "content": [{"type": "text", "text": "explain X"}]},
  {"turn_index": 2, "role": "assistant", "content": [{"type": "text", "text": "X is..."}]}
]
```

Client reconciliation on connect: subscribe WS first, buffer incoming events, then fetch
/history. Discard any buffered events whose `turn_index` ≤ the last turn in the history
response. Remaining buffered events are the in-flight partial turn.

### WebSocket Handshake

On connect, server sends a `session_info` event before any other events:
```json
{"type": "session_info", "data": {"protocol_version": 1, "model": "eu.anthropic.claude-sonnet-4-6", "session_id": "archie-01kwt0xap5"}}
```

### Container Mounts (added by `start`)

- `-v ~/.archie/nexus.yaml:/archie/config/nexus.yaml:ro`
- `-v ~/.aws:/home/{username}/.aws:ro`

---

## Milestones

1. **Create `archie-shared` package with wire protocol and core types**

   Approach:
   - New workspace member at `shared/` with hatchling build backend (same as other packages)
   - Port `types.py` from nextgen (TextBlock, ToolUseBlock, ToolResultBlock) — these are
     used by both agent and future tools
   - Define wire protocol events as frozen dataclasses with `to_json()`/`from_json()`
     methods. Use a `type` discriminator field for deserialization dispatch
   - Include a `SessionInfo` event sent on WS connect (protocol version, model, session_id)
     for forward-compatible handshaking
   - Port `models.py` from nextgen (ModelInfo, MODELS catalog, get_model_info,
     calculate_cost)
   - Port config loading from nextgen — change path to `~/.archie/nexus.yaml`, strip
     sandbox/memory/ollama sections for now (just model, region, project_root)
   - Add `archie-shared` as a workspace dependency of both `cli` and `agent` via
     `[tool.uv.sources]`
   - ⚠️ The shared package must have zero heavy dependencies (no boto3, no textual) —
     only pyyaml
   - ⚠️ Shared source is baked into the Docker image (not dev-mounted like agent). It
     changes infrequently. The Dockerfile must copy workspace root pyproject.toml +
     shared/ + agent/pyproject.toml and run `uv sync --package archie-agent` for
     workspace-aware dependency resolution

   Tasks:
   - Create `shared/pyproject.toml` with pyyaml dependency
   - Create `shared/src/archie_shared/__init__.py` re-exporting public API
   - Port `types.py` (ContentBlock types)
   - Create `events.py` with server→client events (SessionInfo, TextDeltaEvent,
     UsageUpdated, TurnComplete, TurnInterrupted, TurnError) and client→server messages
     (MessageCommand, InterruptCommand), each with `to_json()`/`from_json()`
   - Port `models.py` (ModelInfo, MODELS dict, get_model_info, calculate_cost)
   - Create `config.py` (Config dataclass, load_config reading `~/.archie/nexus.yaml`)
   - Add `archie-shared` to workspace root `pyproject.toml` members and as source dep
     in cli and agent
   - Update Dockerfile: copy workspace root pyproject.toml, shared/ directory, and
     agent/pyproject.toml. Change `uv sync` to `uv sync --package archie-agent --no-dev`
     for workspace-aware resolution that excludes CLI deps (textual, websockets must NOT
     end up in the image). Note: `--no-dev` is Dockerfile-only. Local development
     continues to use `uv sync` (no flags) which installs all workspace packages + dev
     deps (pytest, ruff, etc.)
   - Run `uv sync`, verify imports work from both cli and agent packages

   Deliverable: Both `archie-cli` and `archie-agent` can `from archie_shared import ...`
   and access events, types, models, and config.

   Verify: `uv run python -c "from archie_shared.events import TextDeltaEvent; from archie_shared.models import get_model_info; from archie_shared.config import load_config; print('ok')"` succeeds. After `archie build`, verify inside container: `python -c "import archie_shared"` works AND `python -c "import textual"` fails (confirming CLI deps aren't baked in).

2. **Implement Bedrock streaming client in agent package**

   Approach:
   - Port `llm/bedrock.py` from nextgen into `agent/src/archie_agent/llm/bedrock.py`
   - Port the `LLMClient` protocol into `agent/src/archie_agent/llm/__init__.py`
   - Keep the synchronous generator pattern. The agent app will call it via
     `asyncio.to_thread`
   - Strip nextgen's payload logging for now. Keep the retry logic and cache point
     handling
   - ⚠️ boto3 is an agent-only dependency — do not add it to shared or cli
   - ⚠️ The Turn type used by the Bedrock client needs content blocks from
     `archie_shared.types` — define Turn in the agent's session module, importing
     ContentBlock types from shared
   - ⚠️ Port fix: the Bedrock EventStream must be explicitly closed in a `finally` block.
     nextgen relies on GC (short-lived process), but nexus is a long-lived server —
     abandoned streams leak HTTP connections. The `stream()` generator must use
     `try/finally` around the event iteration to ensure `response["stream"].close()` on
     both normal completion and early exit (interrupt/exception)
   - Note: the LLM stream event types (TextDelta, ToolUseEvent, Usage, Done) in
     `agent/llm/` are internal to the agent. They are NOT the same as wire protocol events
     in `archie_shared.events`. The agent loop translates between them (internal →
     wire) before broadcasting. This is correct layering — internal events carry
     provider-specific detail, wire events are client-facing

   Tasks:
   - Add boto3 to agent dependencies
   - Create `agent/src/archie_agent/llm/__init__.py` with LLMClient protocol and stream
     event types (TextDelta, ToolUseStart, ToolUseEvent, Usage, Done)
   - Port `agent/src/archie_agent/llm/bedrock.py` (BedrockClient with stream/invoke,
     retry logic, cache point handling, explicit stream close in finally)
   - Create `agent/src/archie_agent/session.py` with Turn and Session classes (simplified
     from nextgen — no memory extraction)
   - Write a unit test that mocks boto3 and verifies the stream generator yields correct
     event types

   Deliverable: BedrockClient.stream() yields typed events from a mocked Bedrock response.

   Verify: `uv run pytest tests/test_bedrock.py` passes with mocked boto3 client.

3. **Implement agent loop with event broadcasting**

   Approach:
   - Port the agent loop from nextgen `agent.py`, stripped down to text-only (no tool
     dispatch, no tool-use loop iteration)
   - The loop: receive message → add to session → call LLM stream → emit events → on
     Done, add assistant turn to session → emit TurnComplete
   - Async/sync bridge: a background thread runs the synchronous Bedrock generator and
     pushes each event to the async event loop via `loop.call_soon_threadsafe(queue.put_nowait, event)`.
     The thread captures the running loop before spawning. The async `handle_message`
     method awaits `queue.get()` in a loop and broadcasts each event to connected clients
   - Interrupt handling: the interrupt flag (`threading.Event`) is set by the WS read
     task when it receives an interrupt command. The WS read loop runs independently of
     the queue drain — it is NOT blocked behind `handle_message`. This is critical:
     interrupt must be receivable while the drain loop is awaiting the queue.
     `handle_message` clears the interrupt flag at entry to prevent stale flags from a
     previous turn leaking forward
   - Support interruption via a `threading.Event` flag — the background thread checks it
     between generator yields. When set, stop consuming the generator, close the
     EventStream, and push an internal sentinel (e.g. `None`) to the queue. The sentinel
     is internal-only — the async drain loop receives it, then emits the wire
     `TurnInterrupted` event to clients and stops draining. Two competing signals don't
     reach the client; only the wire event does
   - One active turn at a time — if a message arrives while a turn is in progress, reject
     with a TurnError event broadcast to all clients
   - Each turn increments a monotonic `turn_index` (int) per user message. The user
     message and its assistant response share the same index. All wire events for that
     exchange carry the index, enabling client-side reconciliation on connect

   Wiring:
   - State: `AgentLoop` holds Session, BedrockClient, interrupt flag, event queue,
     connected clients set, turn-active flag
   - Producers: WS message handler calls `handle_message()`, interrupt handler sets flag
   - Consumers: WS broadcast reads from event emission, history endpoint reads
     Session.turns
   - Call site: `await agent.handle_message(content)` triggered by WS message receipt

   Edge cases:
   - Message received while turn active: broadcast TurnError to all clients
   - Bedrock throttling: retry handles it internally, no event emitted until success or
     final failure
   - Interrupt during stream: set flag, thread stops consuming, closes EventStream,
     pushes TurnInterrupted, handler repairs history (adds partial text as assistant
     turn). Interrupt repair is text-only in plan 002 — tool-pairing repair (handling
     unpaired ToolUseBlocks) lands in plan 003
   - Interrupted turns may have no UsageUpdated event (Usage arrives in Bedrock's
     metadata event before Done — interrupt before metadata means no usage data). Status
     bar shows stale values for that turn. Acceptable for v1
   - Bedrock raises unrecoverable exception: emit TurnError with message, reset state
   - Multi-client ownership model: this is a single-user tool. Any connected client can
     send messages or interrupt. An interrupt from any client cancels the turn for all.
     This is intentional — multiple clients are multiple views of the same session, not
     independent actors
   - Session JSONL flush: turns are flushed only on TurnComplete or TurnInterrupted
     (with `interrupted: true`). A crash mid-turn loses that turn — acceptable since
     container crash is abnormal. No incremental flush
   - Credential expiry: expired AWS creds surface as a TurnError with the boto3 exception
     message. No in-session credential refresh in v1

   Tasks:
   - Create `agent/src/archie_agent/agent.py` with AgentLoop class
   - Implement `handle_message(content)` — builds messages, spawns stream thread, awaits
     queue, broadcasts events
   - Implement `_stream_worker(content, queue, interrupt_flag)` — synchronous method that
     runs in thread, reads generator, pushes events to queue
   - Implement interrupt support (threading.Event + queue sentinel)
   - Implement `broadcast(event)` — serializes and sends to all connected WebSocket
     clients
   - Implement system prompt building (minimal for now — just model identity + project
     name)

   Deliverable: AgentLoop receives a message, calls Bedrock, and emits serialized events
   to connected clients.

   Verify: Integration test with mocked Bedrock that verifies events are emitted in
   correct order (TextDeltaEvent, UsageUpdated, TurnComplete). Interrupt test uses a
   slow-yielding mock (with `time.sleep` between chunks) so the interrupt lands
   mid-stream, exercising the thread/queue/flag path and confirming no stale-flag leakage
   into the next turn.

4. **Wire WebSocket and HTTP endpoints into Starlette app**

   Approach:
   - Extend the existing `app.py` to add `WS /stream` and `GET /history` routes
   - WS handler: on connect, add to clients set, send `SessionInfo` event (protocol
     version, model, session_id). Read incoming messages (message/interrupt). On
     disconnect, remove from set
   - History endpoint: serialize Session.turns as JSON array using content-block format:
     `{"role": "user", "content": [{"type": "text", "text": "..."}]}` — forward-compatible
     with tool blocks in plan 003
   - The agent loop is instantiated at app startup (lifespan handler) using config loaded
     from the mounted path
   - Config path: `ARCHIE_CONFIG` env var (set by docker run via `-e`), defaulting to
     `~/.archie/nexus.yaml` for local dev

   Edge cases:
   - Client connects mid-turn: ordering is subscribe WS first, then fetch /history.
     Client buffers events by `turn_index`. After history arrives, discard buffered events
     whose `turn_index` ≤ the last history turn. Remaining events are the in-flight
     partial turn and are replayed into the UI. If a turn completes during the race
     window, it appears in both history and buffered events — the `turn_index` comparison
     handles this cleanly
   - All clients disconnect: agent continues running, turn completes, history is preserved
   - Malformed client message: log warning, ignore
   - Idle connection keepalive: server sends WebSocket ping every 20s to detect dead
     connections (websockets client auto-responds to pings)

   Tasks:
   - Add WebSocket route at `/stream` with connect/disconnect/message handling
   - Send `SessionInfo` event to newly connected clients
   - Add `GET /history` returning session turns as JSON (content-block array format)
   - Instantiate AgentLoop in Starlette lifespan (load config, create BedrockClient,
     create Session)
   - Wire WS message receipt to `agent.handle_message()` and interrupt
   - Update `GET /status` to include session metadata (model, turn count)
   - Write integration test using Starlette TestClient WebSocket support with mocked
     Bedrock that verifies connect → send message → receive events flow

   Deliverable: A running agent container accepts WebSocket connections, processes
   messages, and streams Bedrock responses to all clients.

   Verify: `uv run pytest tests/test_ws_integration.py` passes with mocked Bedrock. Manual
   verify: `websocat ws://127.0.0.1:{port}/stream` connects, send
   `{"type":"message","data":{"content":"hello"}}`, receive events.

5. **Update CLI `start` command with config and credential mounts**

   Approach:
   - Add `-v ~/.archie/nexus.yaml:/archie/config/nexus.yaml:ro` to docker run
   - Add `-v ~/.aws:/home/{user}/.aws:ro` for Bedrock credentials
   - Add `-e ARCHIE_CONFIG=/archie/config/nexus.yaml` so the agent knows where to find
     config (single source of truth — not set in entrypoint.sh)
   - Create default `~/.archie/nexus.yaml` if it doesn't exist (same pattern as nextgen)
   - ⚠️ The .aws mount path inside the container must match the runtime user's home — use
     `/home/{username}/.aws`
   - ⚠️ The shared package is baked into the image (handled in milestone 1 Dockerfile
     changes). No additional mount needed for shared.

   Edge cases:
   - `~/.archie/nexus.yaml` doesn't exist: create with defaults before starting container
   - `~/.aws` doesn't exist: warn but don't fail (user may configure credentials
     differently)
   - AWS SSO/short-lived credentials: a `:ro` mount captures point-in-time state. Token
     expiry mid-session results in Bedrock 403s surfaced as TurnError. No in-session
     refresh path in v1 — user must restart the session after `aws sso login`

   Tasks:
   - Update `start()` to create default config if missing
   - Add config mount to docker_cmd
   - Add .aws mount to docker_cmd
   - Add `-e ARCHIE_CONFIG=/archie/config/nexus.yaml` to docker_cmd

   Deliverable: Container starts with config and AWS credentials available.

   Verify: `archie start` succeeds and `archie shell` → `cat /archie/config/nexus.yaml &&
   aws sts get-caller-identity` both work inside the container.

6. **Implement Textual TUI with WebSocket client**

   Approach:
   - Port the UI layer from nextgen (`ui/` directory) into `cli/src/archie_cli/tui/`
   - Strip all agent-direct coupling: no AgentLoop import, no sandbox, no memory, no
     model switching, no shell commands
   - Keep: Conversation widget (streaming markdown), MessageInput, StatusBar,
     ProjectHeader, Throbber, theme
   - Create `ws_client.py` — async WebSocket client wrapper that connects, handles
     reconnection, deserializes events, and exposes `send_message()`/`send_interrupt()`
   - The TUI's event dispatch: WS message arrives → deserialize via
     `archie_shared.events.from_json()` → dispatch to widget updates (same switch as
     nextgen's `_handle_event`)
   - On startup: connect WS, fetch `/history`, replay turns into conversation widget
   - Add `textual` and `websockets` to CLI dependencies
   - ⚠️ Port the `.tcss` stylesheet from nextgen — it controls the layout
   - ⚠️ Textual's async model: WS messages arrive on a task, post Textual Messages to
     marshal to the widget thread

   Tasks:
   - Add textual and websockets to cli/pyproject.toml
   - Create `cli/src/archie_cli/ws_client.py` (connect, reconnect, send, receive,
     deserialize). On connect: subscribe WS, buffer events with turn_index, fetch
     /history, discard buffered events with turn_index ≤ last history turn, replay
     remainder as in-flight partial turn. On `SessionInfo` receipt: log warning if
     `protocol_version` != expected (no hard rejection for v1). The `session_id` in
     SessionInfo matches the container name suffix used by `archie attach` prefix matching
   - Port `conversation.py` (Conversation widget, StreamingMessage)
   - Port `input.py` (MessageInput)
   - Port `status.py` (StatusBar)
   - Port `theme.py` and `archie.tcss`
   - Port `throbber.py`
   - Create `cli/src/archie_cli/tui/app.py` — ArchieApp connecting via WS instead of
     direct agent
   - Implement history catch-up on connect (GET /history → populate conversation)
   - Implement Esc → send interrupt over WS

   Deliverable: `archie attach` launches a TUI that connects to a running session and
   displays streaming conversation.

   Verify: Start a session (`archie start`), attach (`archie attach`), type a message, see
   streaming response rendered in the TUI.

7. **Add `archie attach` command to CLI**

   Approach:
   - Same session resolution pattern as `shell` (prefix matching, picker for ambiguous)
   - Resolve session → get port → launch TUI app with `host="127.0.0.1"` and
     `port=<resolved_port>`
   - The TUI app receives host/port at construction and builds the WS URL from it

   Tasks:
   - Add `attach` command to cli.py with session_id argument (optional, same pattern as
     shell)
   - Resolve target session and port (reuse existing `list_sessions` + prefix match logic)
   - Instantiate and run the ArchieApp with the resolved connection details

   Deliverable: `archie attach [session_id]` launches the TUI connected to the specified
   session.

   Verify: `archie start && archie attach` launches the TUI, full conversation round-trip
   works.
