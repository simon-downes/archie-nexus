# Archie Nexus session protocol walkthrough

This document explains, from first principles, what happens between the terminal UI (TUI), the
orchestrator, and the agent during an Archie Nexus session. It follows a new session from creation
and connection through a multi-iteration turn with streamed text and tool calls, then explains
session-log storage, client replay, reconnection, errors, and child agents.

For the exact field-level contract, see [event-spec.md](event-spec.md). For the wider system
architecture, see [architecture.md](architecture.md).

## 1. The three components

```text
┌──────────────────┐       HTTP + WebSocket       ┌──────────────────┐
│ TUI              │ ◀──────────────────────────▶ │ Orchestrator     │
│                  │                              │                  │
│ User interface   │                              │ Session lifecycle│
│ _apply_event()   │                              │ Transparent proxy│
│ Replay cursor    │                              │ Metrics index    │
└──────────────────┘                              └────────┬─────────┘
                                                         │
                                                         │ HTTP + WebSocket
                                                         │ via localhost port
                                                         ▼
                                                ┌──────────────────┐
                                                │ Agent container  │
                                                │                  │
                                                │ LLM loop         │
                                                │ Tools/subagents  │
                                                │ SessionEventBus  │
                                                │ SessionLog       │
                                                └──────────────────┘
```

### TUI

The TUI is the client the user sees. It:

- sends strict flat client commands;
- receives live events over a WebSocket;
- reads persisted history over HTTP;
- applies history through `_render_canonical(event, replay=True)` and live events through `_apply_event()`, which dispatches into the same renderer;
- renders user messages, assistant text, tools, child agents, and errors;
- keeps the last applied persisted event ID as its client replay cursor;
- calculates displayed token and cost totals from `llm_request` events.

The TUI is not the source of truth. It does not immediately add submitted prompts to the
conversation and it does not invent persisted events. It waits for the agent's canonical
`user_message` event.

### Orchestrator

The orchestrator is the host-side control plane. It:

- starts and stops agent containers;
- resolves session IDs to running containers and their mapped ports;
- proxies session HTTP requests to the correct agent;
- relays WebSocket traffic in both directions without interpreting commands;
- observes live `llm_request` frames and writes a best-effort SQLite metrics index.

The orchestrator does not run the LLM loop, execute tools, create public events, or own the
canonical session log.

### Agent

One agent process owns one session. It:

- accepts commands from connected clients;
- allows at most one active root turn;
- calls the model provider;
- executes tools and child agents;
- creates all server-to-client events;
- orders events through one `SessionEventBus`;
- writes persisted events through one `SessionLog` before broadcasting them.

The session JSONL log is the source of truth for conversation history and request accounting.

## 2. Two communication paths

The TUI uses both WebSocket and HTTP because they solve different problems.

### WebSocket: commands and live events

```text
ws://<orchestrator>/sessions/<session-id>/stream
```

The WebSocket is bidirectional:

- **TUI to agent:** flat `message`, `interrupt`, and `switch_model` commands;
- **agent to TUI:** public events, including streamed `text_delta` events.

The orchestrator transparently relays this connection to the agent's `/stream` endpoint. It does
not translate the command or event schema.

### HTTP: persisted history

```text
GET http://<orchestrator>/sessions/<session-id>/events
GET http://<orchestrator>/sessions/<session-id>/events?after=<event-id>
```

The HTTP endpoint reads persisted events as newline-delimited JSON (NDJSON), in session-log order.

- No `after` parameter means read the complete persisted log.
- `after=<id>` means read every persisted event after that event.
- An unknown cursor returns HTTP `409` with `cursor_not_found`.

HTTP history reads do not include live-only events such as `text_delta`; those events are never
written to the session log. The client may replay the events it read through its normal rendering
path, but storage access itself is a log read.

## 3. Public commands and events

Public event types and client commands are separate contracts in the shared package:

```text
shared/src/archie_shared/events.py
shared/src/archie_shared/commands.py
```

Both use strict flat tagged `msgspec.Struct` records. Commands do not contain a nested `data`
object. Unknown fields and unknown tags are rejected.

Examples of valid commands are:

```json
{"type":"message","content":"Inspect the configuration."}
{"type":"interrupt"}
{"type":"interrupt","scope":"task-01","subagent_index":0}
{"type":"switch_model","model_key":"bedrock-anthropic.claude-sonnet-4-6"}
```

An `interrupt` command either targets the root with neither child field or targets a child with
both `scope` and `subagent_index`. A partial child target is invalid.

Every server-to-client event has a flat JSON object with a `type`, an `id`, and fields specific to
that event type. For example:

```json
{
  "type":"text_delta",
  "id":"01J...",
  "turn":1,
  "iteration":0,
  "scope":null,
  "subagent_index":null,
  "request_id":"01J...",
  "text":"I will inspect the file."
}
```

All event IDs are ULID-like values. The event bus determines delivery order and the session log's
append order determines stored order. Clients do not use wall-clock timestamps as cursors.

### Persisted events versus live-only events

The protocol has one public event union. The classification is declarative on each event class:

| Class | Events | Meaning |
| --- | --- | --- |
| Persisted | `session_started`, `user_message`, `iteration_start`, `llm_request`, `assistant_message`, `tool_call`, `tool_result`, `turn_complete`, `turn_error`, `turn_interrupted` | Written to JSONL before live delivery; available to client replay and history reads |
| Live-only | `handshake`, `session_status`, `text_delta`, `error_notice` | Sent over the WebSocket but never written to JSONL |

There is no parallel replay schema. A persisted event has the same shape whether the TUI receives
it live over WebSocket or later from `GET /events`.

## 4. Creating a new session

When the user runs `archie start`, the CLI first creates the session:

```text
TUI/CLI                    Orchestrator                  Agent container
   │                            │                              │
   │ POST /sessions            │                              │
   │ {"workspace":"project"}  │                              │
   ├───────────────────────────▶│                              │
   │                            │ start Docker container       │
   │                            ├─────────────────────────────▶│
   │                            │                              │ initialize agent
   │                            │                              │ create SessionLog
   │                            │                              │ create SessionEventBus
   │                            │                              │ append session_started
   │                            │ poll /status until ready     │
   │                            ├─────────────────────────────▶│
   │ SessionDescriptor         │◀─────────────────────────────┤
   │◀───────────────────────────┤                              │
```

The orchestrator validates the workspace, creates a session ID, starts the container, maps its port
8080 to an ephemeral localhost port, and waits for `/status` to report ready.

During agent startup, `SessionLog` opens the session JSONL file. If the log is empty, the agent
publishes the first event through `SessionEventBus.emit()`:

```text
session_started   persisted
```

There are no WebSocket clients yet, so the TUI normally learns this event through its initial
history read. `session_started` records the log schema version, initial model key, and session
start time.

Once the orchestrator returns a `SessionDescriptor`, the CLI starts the TUI with two URLs:

```text
WebSocket: ws://<orchestrator>/sessions/<session-id>/stream
HTTP base: http://<orchestrator>/sessions/<session-id>
```

`archie attach` reaches the same connection flow for an already-running session.

## 5. Initial TUI connection

The connection procedure combines live subscription and a history read. The goal is to avoid
losing events produced while the log is being read.

### Step 1: open the WebSocket

The TUI connects to the orchestrator's session WebSocket. The orchestrator:

1. resolves the session ID to a running container;
2. accepts the TUI connection;
3. opens a second WebSocket to the agent's `/stream` endpoint;
4. starts one relay task in each direction.

```text
TUI WebSocket ───── Orchestrator relay ───── Agent WebSocket
```

Command and event payloads pass through unchanged.

### Step 2: register the client

The agent accepts the backend WebSocket and atomically does three things through
`SessionEventBus`:

1. registers the socket;
2. queues a `handshake` event;
3. queues a `session_status` event.

The first two server frames are therefore:

```text
handshake        live-only
session_status   live-only
```

`handshake` contains connection metadata:

```json
{
  "type":"handshake",
  "id":"01J...",
  "protocol_version":2,
  "session_id":"..."
}
```

`session_status` carries live runtime status such as the active model key and Git branch. Neither
event is a history snapshot, accounting source, or replay cursor. `handshake` is always followed by
`session_status`; model changes are represented by a later `session_status` event.

Registration and these two events happen under the coordinator's ordering lock. No concurrently
produced event can be placed ahead of the handshake or between the handshake and initial status for
this client.

### Step 3: buffer live events

Immediately after connecting, the TUI starts its WebSocket receive loop in buffering mode. Anything
arriving from the agent is temporarily held rather than rendered. This includes the handshake and
status events, and can include turn events if another attached client starts a turn at the same
time.

### Step 4: read persisted history

While live events are buffered, the TUI requests:

```text
GET /sessions/<session-id>/events
```

For a new session, the response normally contains only `session_started`. For an existing session,
it contains the complete persisted history. Each NDJSON line is decoded through the same public
event decoder used for live events and passed to `_render_canonical(event, replay=True)`.

The TUI records every applied persisted event ID in a deduplication set. Its cursor advances only
when the shared renderer accepts a persisted event; live-only events never advance it.

### Step 5: apply the buffered live events

After the history read succeeds, the TUI leaves buffering mode and applies the buffered WebSocket
events through `_apply_event()`. Events already applied from history are ignored by event ID or,
for assistant text, by exact request identity.

The connection order is therefore:

```text
1. Subscribe to live events
2. Buffer anything received
3. Read and apply persisted history
4. Replay buffered live events through _apply_event(), deduplicated by identity
5. Continue applying new live events directly
```

Subscribing before reading history closes the live/history race. An event persisted during the HTTP
request will be present in the history response, the WebSocket buffer, or both. If present in both,
the identity rules make the second copy harmless.

## 6. Sending a user prompt

Suppose the user enters:

```text
Inspect the configuration, run the relevant tests, and explain the result.
```

### What the TUI does locally

The TUI:

1. marks the turn as active;
2. disables the input;
3. shows a waiting indicator;
4. sends a flat WebSocket command.

The command is:

```json
{"type":"message","content":"Inspect the configuration, run the relevant tests, and explain the result."}
```

The TUI does not add this text to the conversation yet. This prevents optimistic local content from
being duplicated or disagreeing with canonical history.

### What the orchestrator does

The orchestrator forwards the text frame unchanged to the agent. It does not decode the prompt or
make a turn-admission decision.

### What the agent does

The agent decodes the command and atomically calls `try_begin_turn()`.

- If no root turn is active, the prompt is accepted and processing starts in a background task.
- If a root turn is already active, the submitting client receives a live-only `error_notice` with
  `kind="turn_active"`. The rejection is not persisted and other connected clients are unaffected.

For an accepted turn, the harness allocates the next integer turn number and publishes:

```text
user_message   persisted
```

`SessionEventBus.emit()` first appends the event to the session log, then queues exactly that
serialized event to every connected client. All attached TUIs therefore see the same initiating
prompt, regardless of which client submitted it.

The submitting TUI renders the prompt only after receiving this canonical event.

## 7. Turns, iterations, requests, and tools

These terms describe different levels of work:

- A **turn** begins with one accepted user prompt and ends with one terminal event.
- An **iteration** is one pass through the model/tool loop.
- An **LLM request** is one provider call inside an iteration.
- A **tool call** is work requested by the model after an LLM request.

A request identity is the exact tuple:

```text
(scope, subagent_index, turn, iteration, request_id)
```

Root events use `scope=null` and normally `subagent_index=null`. Child events use the launching
task's scope and a child index. `request_id` identifies the provider request for that iteration;
it is singular, not a cumulative list.

## 8. One model iteration in detail

Each iteration starts by publishing:

```text
iteration_start   persisted
```

The iteration number starts at `0`. The agent allocates the request ID before invoking the provider,
then streams the provider response.

### While the provider is streaming

Text chunks are immediately broadcast as:

```text
text_delta   live-only
```

Every delta identifies its turn, iteration, scope, child index where applicable, and request ID. A
connected TUI appends the text to an in-progress assistant widget, giving the user immediate
feedback.

The provider can also stream tool-use information. The loop collects that information internally;
public `tool_call` events are emitted only after the provider request has finished.

### When the provider stream finishes

Only now does the agent know the complete request outcome, including:

- duration;
- input and output token usage;
- cache token usage;
- context tokens;
- request status;
- stop reason;
- immutable request cost;
- any provider error.

It therefore publishes one complete ledger record:

```text
llm_request   persisted
```

This explains why `llm_request` follows streamed `text_delta` events. It describes the completed
provider request; it is not a request-start notification. There is no public `usage` event. Provider
usage is folded into `llm_request`.

### If the model requested tools

If the iteration produced assistant text, the agent first publishes:

```text
assistant_message   persisted, optional
```

The record contains only the text produced by this iteration and its exact identity:

```text
assistant_message(
  scope=<scope>,
  subagent_index=<index>,
  turn=<turn>,
  iteration=<iteration>,
  request_id=<request_id>,
  content=<iteration text>,
  interrupted=false
)
```

This is the authoritative durable copy of text previously shown through live-only `text_delta`
events. It is not a snapshot of the whole child or turn history.

The agent then publishes each requested tool:

```text
tool_call   persisted
```

Tools in one model response are announced in model order. The agent may execute the batch
concurrently. As each tool finishes, it publishes:

```text
tool_result   persisted
```

Concurrent results can arrive in completion order rather than tool-call order. Each result carries
the same `tool_use_id` as its call, allowing the TUI to update the correct pending tool row.

After the batch, the agent adds the assistant's tool requests and tool results to its in-memory model
transcript, then begins the next iteration.

### If the model produced a final answer

If no further tool use is required, the agent publishes an `assistant_message` for any final text,
then publishes:

```text
turn_complete   persisted
```

The accepted turn now has its one durable terminal event.

## 9. Complete multi-iteration example

Assume one turn has two tool iterations followed by a final answer. Request IDs are shown as `r1`,
`r2`, and `r3`; actual IDs are ULIDs. `P` means persisted and replayable. `L` means live-only.

```text
Direction   Event                                           Class
─────────   ─────────────────────────────────────────────   ─────
TUI → agent {"type":"message","content":"..."}          command

agent → TUI user_message(turn=1)                            P

            # Iteration 0: explain, then inspect a file
agent → TUI iteration_start(turn=1, iteration=0)            P
agent → TUI text_delta(request_id=r1, "I will inspect ")    L
agent → TUI text_delta(request_id=r1, "the config.")        L
agent → TUI llm_request(id=r1, iteration=0,                  P
                stop_reason="tool_use")
agent → TUI assistant_message(                              P
                turn=1, iteration=0, request_id=r1,
                content="I will inspect the config.")
agent → TUI tool_call(request_id=r1, tool_use_id=t1,        P
                name="read", input={...})
agent → TUI tool_result(request_id=r1, tool_use_id=t1,      P
                content="...", is_error=false)

            # Iteration 1: explain, then run tests
agent → TUI iteration_start(turn=1, iteration=1)            P
agent → TUI text_delta(request_id=r2, "Now I will run ")    L
agent → TUI text_delta(request_id=r2, "the tests.")         L
agent → TUI llm_request(id=r2, iteration=1,                  P
                stop_reason="tool_use")
agent → TUI assistant_message(                              P
                turn=1, iteration=1, request_id=r2,
                content="Now I will run the tests.")
agent → TUI tool_call(request_id=r2, tool_use_id=t2,        P
                name="exec", input={...})
agent → TUI tool_result(request_id=r2, tool_use_id=t2,      P
                content="42 passed", is_error=false)

            # Iteration 2: final answer, no more tools
agent → TUI iteration_start(turn=1, iteration=2)            P
agent → TUI text_delta(request_id=r3, "The configuration ") L
agent → TUI text_delta(request_id=r3, "is valid...")        L
agent → TUI llm_request(id=r3, iteration=2,                  P
                stop_reason="end_turn")
agent → TUI assistant_message(                              P
                turn=1, iteration=2, request_id=r3,
                content="The configuration is valid...")
agent → TUI turn_complete(turn=1, stop_reason="end_turn")   P
agent → TUI session_status(model_key="...", git_branch="...") L
```

The successful iteration pattern is:

```text
iteration_start
text_delta*
llm_request
assistant_message?       # only if this iteration produced text
tool_call*
tool_result*             # when tools were requested
```

The final iteration ends with:

```text
llm_request
assistant_message?
turn_complete
```

A later `session_status` may report branch or model changes, but it is live-only and is not part of
the turn's persisted event sequence.

## 10. How the TUI applies the events

The TUI uses one imperative `_render_canonical()` path for events received from both live WebSocket
delivery and HTTP history reads. Live `_apply_event()` dispatches into that renderer; history reads
call it directly with `replay=True`. The TUI does not maintain a second reducer or a separate replay
schema.

### User messages

`user_message` adds the canonical prompt to the conversation and resets per-turn display metrics.

### Streamed assistant text

For a root `text_delta`, `_apply_event()`:

1. removes the waiting indicator;
2. creates an in-progress assistant widget if necessary;
3. appends the chunk;
4. stores the temporary text by exact request identity.

For child text it does the same in the matching child view. The temporary record is keyed by
`(scope, subagent_index, turn, iteration, request_id)`, so sibling streams and interleaved root/child
streams cannot append to each other's widgets.

When an authoritative `assistant_message` arrives:

- if its exact request identity has matching temporary text, `_apply_event()` finalizes that widget;
- if the text differs, it removes the temporary widget and renders the persisted content;
- if there was no temporary widget, as during ordinary history reads, it renders the persisted
  content directly;
- it records the identity in a finalized map, so later deltas for that request are suppressed.

The persisted event always wins. `text_delta` exists for responsiveness, not durability. A cumulative
whole-turn request-ID list is not used; each assistant record has one direct `request_id` and one
integer `iteration`.

### Tools

`tool_call` creates a pending tool row and remembers structured input by `tool_use_id` within its
scope. `tool_result` finds that row, derives a human-readable summary from the call and result, and
marks it complete.

`iteration_start` does not immediately create a visible block. The TUI creates an iteration block
lazily only if a tool call appears, avoiding empty blocks for text-only iterations.

### Accounting

Each `llm_request` is folded into cumulative input, output, cache, and cost totals. Event IDs are
deduplicated so receiving the same ledger event once through a history read and once through live
delivery does not double-count it.

The TUI does not calculate prices from current model rates. It sums the immutable `cost_usd` recorded
by the agent in each request event.

### Turn and child completion

`turn_complete`, `turn_error`, or `turn_interrupted` ends the local turn display, removes temporary
widgets, and re-enables input for a root turn. Scoped terminal events close only the matching child
view. Only one of these persisted terminal events should exist for an accepted scope and turn,
except when the storage append path itself has failed.

## 11. The session event coordinator

Every root event and child event passes through one `SessionEventBus` inside the agent. This is the
sole public-event sink. `register_client()` is the atomic connection-admission exception because it
must register a socket and queue the initial live status frames together.

### Publishing a persisted event

For a persisted event, `SessionEventBus.emit()` performs:

```text
construct event
  → acquire ordering lock
  → serialize event
  → SessionLog.append(event)
  → update in-memory ID index
  → enqueue the same serialized line for every client
  → release ordering lock
```

The append happens before enqueueing. Every persisted event seen live therefore already has a
session-log entry.

`SessionLog.append()` validates event IDs and event shape. An identical event ID with identical
serialized content is an idempotent duplicate. Reusing an ID with different content is an error.
`SessionLog.read()` preserves append order and skips malformed or unrecognised records with a warning.

### Broadcasting a live-only event

For `handshake`, `session_status`, `text_delta`, or `error_notice`, the coordinator skips the
append and performs ordered queue admission only.

### Slow clients do not block the session

Each connected agent-side WebSocket has its own bounded queue and sender task. Network writes happen
outside the ordering lock. A slow client therefore cannot delay log persistence, provider processing,
or other clients.

If a client queue exceeds 1024 frames, the coordinator disconnects that client so it can reconnect
and recover persisted history.

### Multiple clients see one ordered stream

Persisted root and child events serialize through the same ordering lock. Every healthy attached
client is enqueued the same frames in the same order. A second client also receives the initiating
`user_message`, not just the response.

## 12. What the orchestrator does with events

For WebSocket traffic, the orchestrator runs two independent relay tasks:

```text
TUI text frames   → agent
agent frames      → TUI
```

On the agent-to-TUI path, it performs a cheap marker check for accounting events. A live
`llm_request` frame is copied into a bounded metrics queue while the original frame continues to the
TUI. Metrics ingestion is best-effort: a metrics failure must not delay or break the session relay.

The SQLite metrics database is an index for aggregate queries. It is not the canonical event store.
The JSONL session log remains authoritative.

For `GET /events`, the orchestrator forwards the optional `after` cursor unchanged and relays the
agent's status code, body, and content type.

## 13. Reconnection and client replay

A WebSocket can disappear while the agent continues working. The recovery procedure is the same as
initial connection, except the TUI normally has a persisted cursor.

### The cursor

The TUI tracks the ID of the last persisted event it applied. Live-only events never advance the
cursor because their IDs do not exist in the log.

For example, after applying:

```text
iteration_start (persisted)
text_delta      (live-only)
text_delta      (live-only)
```

the cursor still points to `iteration_start`.

### Reconnect sequence

After a lost connection, the TUI retries with backoff for up to roughly 30 seconds:

```text
1. Open a new WebSocket
2. Start buffering new live frames
3. Receive a new handshake and session_status
4. GET /events?after=<last-persisted-event-id>
5. Apply missed persisted events in log order through _render_canonical(event, replay=True)
6. Flush buffered live events through _apply_event(), deduplicated by event ID and request identity
7. Resume direct live processing
```

The new handshake does not replace or advance the history cursor.

### Text deltas lost during disconnection

Lost `text_delta` events cannot be recovered from the log. This is intentional. The durable
`assistant_message` contains the completed text for the iteration, and an interrupted or failed
iteration persists available partial text before its terminal event.

If the provider finishes while the TUI is disconnected, the history read supplies:

```text
llm_request
assistant_message
...later persisted events...
```

The TUI reconstructs the correct final conversation even though it cannot reproduce the original
character-by-character animation.

If buffered post-reconnect deltas belong to a request whose `assistant_message` was already found in
the history read, `_apply_event()` suppresses those deltas using the finalized request-key map.

### Unknown cursor

If the agent returns `409 cursor_not_found`, the log may have been rotated or replaced. The TUI must
not simply read from the beginning on top of its existing state. It first clears all state derived
from client replay, including:

- rendered conversation entries;
- seen event IDs;
- accounting IDs and totals;
- tool and child-agent state;
- temporary and finalized request-key maps;
- the old cursor.

It then requests the complete log and rebuilds from scratch.

## 14. Child agents and event scope

The root model can invoke the `task` tool to run one or more child agents. The parent first emits a
normal persisted root-scoped `tool_call` for `task`.

The launching task call's `tool_use_id` becomes each child's `scope`. `subagent_index` distinguishes
children launched in the same task batch:

```text
Root event:   scope=null
Child event:  scope=<parent task tool_use_id>, subagent_index=0
Sibling:      scope=<same tool_use_id>,         subagent_index=1
```

A child runs its own iteration/request/tool loop and emits scoped versions of:

```text
iteration_start
text_delta
llm_request
assistant_message
 tool_call
 tool_result
turn_complete | turn_error | turn_interrupted
```

Child events retain the parent root turn number but have their own iteration numbers and request IDs.
The complete identity of each assistant request remains
`(scope, subagent_index, turn, iteration, request_id)`.

All child events are decoded and routed through the same `SessionEventBus` as root events. Concurrent
children can interleave, but final append order is also delivery and client replay order. The TUI
routes scoped events into the matching child activity view rather than the root conversation.

The child prompt is internal to task execution; there is no separate public child `user_message`
event. When the child batch finishes, the parent `task` tool produces its own root-scoped
`tool_result`, which becomes context for the parent's next model iteration.

A typical parent/child outline is:

```text
parent tool_call(name="task", tool_use_id=t1)

  child iteration_start(scope=t1, subagent_index=0)
  child text_delta*(scope=t1, subagent_index=0, ...)
  child llm_request(scope=t1, subagent_index=0, request_id=c1)
  child assistant_message(scope=t1, subagent_index=0, request_id=c1)
  child turn_complete(scope=t1, subagent_index=0, ...)

parent tool_result(tool_use_id=t1)
parent iteration_start(next iteration)
...
```

If a child is interrupted or fails after producing text, the child publishes its available partial
text as `assistant_message(interrupted=true)` before the scoped terminal event. The same ordering
rule applies to root turns. This makes partial output durable rather than leaving it only in lost
live deltas.

## 15. Additional protocol flows and boundaries

### Direct shell commands (`!`)

Input beginning with `!` is handled entirely by the host-side TUI. It does not become a `message`
command, enter the LLM/tool loop, use an HTTP shell route, or create a public event.

The TUI removes the prefix and executes the command locally with `docker exec`, using `/workspace`
inside the session container as the working directory:

```text
TUI ──docker exec──▶ session container
TUI ◀──exit code + combined output── session container
TUI ──render local result only──▶ submitting TUI
```

The TUI applies its timeout and output limits and renders the result locally. There is no persistence,
no event-bus emission, no cross-client broadcast, and no history entry for this command.

### Model changes

When idle, the TUI can send the flat command:

```json
{"type":"switch_model","model_key":"..."}
```

The agent performs the authoritative checks. An active turn or unknown model key produces a targeted
live-only `error_notice` for the submitting client.

On success, the agent resolves the model and changes the active runtime model, then emits a live-only
`session_status` event containing the current status. No model-change event is persisted. Other
clients receive the status only through their live connection, and a newly connected client receives
the current status after its `handshake`.

Historical `llm_request` costs are never repriced.

### Transport trust boundary

The built-in session routes use cleartext HTTP/WebSocket and have no authentication or authorization.
A session ID identifies the route but is not an access-control credential. The default orchestrator
profile binds to loopback, and the orchestrator-to-agent connection uses a localhost-mapped container
port.

If the orchestrator is exposed on a non-loopback interface, prompts, history, tool output, interrupt
commands, and model changes require a trusted private network or external transport and access
controls. Nexus does not add TLS or client authentication itself.

## 16. Interruptions, errors, and rejections

### User interruption

Pressing Escape during an active turn sends:

```json
{"type":"interrupt"}
```

A targeted child interruption includes both `scope` and `subagent_index`:

```json
{"type":"interrupt","scope":"task-01","subagent_index":0}
```

The agent signals the provider or tool batch to stop, and the request ledger records an interrupted
outcome where applicable. A root or child scope persists available partial assistant content with
`interrupted=true` before ending with:

```text
turn_interrupted   persisted
```

### Provider or turn failure

A root provider failure normally produces an `llm_request` with error status, followed by available
partial assistant content and:

```text
turn_error   persisted
```

Unexpected root harness failures are converted into one fallback terminal error when storage is still
working. Child failures emit a scoped terminal error after their partial text, if any, has been
persisted.

### Command rejection

Conditions such as a second prompt during an active turn or a model change during a turn are command
rejections, not accepted-turn failures. The agent sends only the submitting client:

```text
error_notice   live-only
```

The event is not persisted because no durable turn transition occurred.

### Storage failure

If the agent cannot append a persisted event, it cannot truthfully create a durable terminal event.
It clears active turn state and makes a best-effort broadcast of:

```text
error_notice(kind="storage_error")   live-only
```

This is the explicit exception to the one-terminal-event guarantee.

## 17. What is and is not recoverable

| Information | Live view | Persisted log | Recoverable after disconnect? |
| --- | --- | --- | --- |
| Session connection metadata | `handshake` | No | Sent again on every connection |
| Model and Git branch status | `session_status` | No | Sent again on connection or status change |
| User prompt | `user_message` | Yes | Yes |
| Iteration boundary | `iteration_start` | Yes | Yes |
| Character-by-character assistant animation | `text_delta` | No | No |
| Completed assistant text from normal iterations | `assistant_message` | Yes | Yes |
| Available partial assistant text on failure/interruption | `assistant_message(interrupted=true)` | Yes | Yes |
| Request usage, duration, status, and cost | `llm_request` | Yes | Yes |
| Tool invocation and result | `tool_call`, `tool_result` | Yes | Yes |
| Accepted-turn outcome | terminal event | Yes | Yes |
| Command rejection | `error_notice` | No | No; it applies only to that live client |

The design sacrifices recovery of the typing animation while preserving normal completed
conversation, available partial output, and all accounting/tool history.

## 18. Protocol invariants

The following rules are useful when reading logs, debugging the TUI, or changing the protocol:

1. **The agent owns public events.** The TUI sends commands; it does not invent conversation history.
2. **The orchestrator is a relay, not the session authority.** It owns container lifecycle and a
   best-effort metrics index.
3. **Persisted events are appended before broadcast.** A live persisted event already exists in the
   JSONL log.
4. **`text_delta` is temporary.** `assistant_message` is the durable authority for assistant text.
5. **Every assistant record has one direct request identity.** The identity is
   `(scope, subagent_index, turn, iteration, request_id)`; there is no cumulative request-ID list.
6. **The client replay cursor names only persisted events.** Never use a handshake, status, delta, or
   error-notice ID as `after=`.
7. **One `_render_canonical()` path handles live and history events.** Live `_apply_event()`
   dispatches into it; event-ID and request-key deduplication makes overlap safe.
8. **`llm_request` is the accounting ledger.** Sum it by unique event ID; do not derive historical
   cost from current prices.
9. **One accepted scope and turn means one terminal event.** Rejections are live notices, not
   terminal events.
10. **Root and child events share one coordinator.** Scope identifies ownership; append order is the
    global client-replay order.
11. **Tools separate iterations.** Their results become model context for the next request.
12. **Partial text precedes terminal failure.** Available root and child text is durable before
    `turn_error` or `turn_interrupted`.

## 19. Compact end-to-end summary

```text
NEW SESSION
CLI ──POST /sessions──▶ orchestrator ──start container──▶ agent
agent ──SessionLog.append(session_started)──▶ JSONL

CONNECT
TUI ──WebSocket──▶ orchestrator ──WebSocket──▶ agent
agent ──handshake, session_status (live-only)──▶ TUI buffer
TUI ──GET /events──▶ orchestrator ──GET /events──▶ agent
agent ──persisted NDJSON history──▶ TUI _render_canonical(replay=true)
TUI ──flush deduplicated live buffer──▶ rendered session

PROMPT
TUI ──flat message command──▶ orchestrator ──relay──▶ agent
agent ──SessionEventBus.emit(user_message)──▶ JSONL
agent ──broadcast user_message──▶ all TUIs

EACH ITERATION
agent ──persist iteration_start──▶ JSONL + TUIs
agent ──broadcast text_delta*──▶ TUIs only
agent ──persist llm_request──▶ JSONL + TUIs
agent ──persist assistant_message?──▶ JSONL + TUIs
agent ──persist tool_call*/tool_result*──▶ JSONL + TUIs

TURN END
agent ──persist turn_complete/error/interrupted──▶ JSONL + TUIs
agent ──broadcast session_status when status changes──▶ TUIs only

RECONNECT
TUI ──new WebSocket; buffer live events──▶ agent
TUI ──GET /events?after=<last persisted id>──▶ agent
TUI ──apply history, then deduplicated live buffer──▶ restored view
```
