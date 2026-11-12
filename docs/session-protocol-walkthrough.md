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
- applies history and live events through the same `_apply_event(event)`;
- renders user messages, assistant text, tools, child agents, and errors;
- keeps the last applied persisted event ID as its client replay cursor;
- calculates displayed token and cost totals from `llm_request` events.

The TUI is not the source of truth. It does not immediately add submitted prompts to the
conversation and it does not invent persisted events. It waits for the agent's canonical
`user_message` event.

### Orchestrator

The orchestrator is the host-side control plane. It:

- starts and stops agent containers;
- keeps one record per session in `archie.db` (status, name, model, pending question, times),
  kept current by a session tracker that follows each running agent's `/stream` read-only;
- resolves session IDs to the agents' mapped ports from the tracker's memory;
- proxies session HTTP requests to the correct agent;
- relays WebSocket traffic in both directions without interpreting commands;
- tails the session logs into a best-effort SQLite metrics index.

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
ws://<orchestrator>/api/sessions/<session-id>/stream
```

The WebSocket is bidirectional:

- **TUI to agent:** flat `message`, `interrupt`, `switch_model`, and `answer` commands;
- **agent to TUI:** public events, including streamed `text_delta` events.

The orchestrator transparently relays this connection to the agent's `/stream` endpoint. It does
not translate the command or event schema.

### HTTP: persisted history

```text
GET http://<orchestrator>/api/sessions/<session-id>/events
GET http://<orchestrator>/api/sessions/<session-id>/events?after=<event-id>
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
{"type":"answer","question_id":"tooluse_01","option":1}
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
| Persisted | `session_started`, `user_message`, `iteration_start`, `llm_request`, `context_compacted`, `assistant_message`, `tool_call`, `tool_result`, `turn_complete`, `turn_error`, `turn_interrupted` | Written to JSONL before live delivery; available to client replay and history reads |
| Live-only | `handshake`, `session_status`, `text_delta`, `error_notice` | Sent over the WebSocket but never written to JSONL |

There is no parallel replay schema. A persisted event has the same shape whether the TUI receives
it live over WebSocket or later from `GET /events`. When context compaction occurs, the agent persists
`llm_request(purpose=compaction)` followed by `context_compacted`; the TUI renders the marker and
summary in both live and replay paths. The replacement transcript is then one compacted user turn,
with a subsequent user message merged into that turn.

## 4. Creating a new session

When the user runs `archie start`, the CLI first creates the session:

```text
TUI/CLI                    Orchestrator                  Agent container
   │                            │                              │
   │ POST /api/sessions         │                              │
   │ {"workspace":"project"}    │                              │
   ├───────────────────────────▶│                              │
   │                            │ validate, insert record      │
   │ 202 SessionDescriptor      │ (status: starting)           │
   │◀───────────────────────────┤                              │
   │                            │ start Docker container       │
   │                            ├─────────────────────────────▶│
   │                            │                              │ initialize agent
   │                            │                              │ create SessionLog
   │                            │                              │ create SessionEventBus
   │                            │                              │ append session_started
   │                            │ poll /status until ready     │
   │                            ├─────────────────────────────▶│
   │                            │ tracker follows /stream      │
   │                            ├─────────────────────────────▶│
   │                            │ session_status: idle         │
   │                            │◀─────────────────────────────┤
   │ GET /api/sessions/<id>     │                              │
   │ every 0.5 s while starting │                              │
   ├───────────────────────────▶│                              │
   │ SessionDescriptor (idle)   │                              │
   │◀───────────────────────────┤                              │
```

The orchestrator validates the workspace and image, creates a session ID, inserts a `starting`
session record (a `SessionDescriptor`, stored in `archie.db`) and returns it at once with 202. In
the background it starts the container, maps its port 8080 to an ephemeral localhost port, and
waits for the agent's own `/status` to report ready. Its session tracker then follows the agent's
`/stream` for the rest of the session; the `session_status` the agent sends on connect makes the
record `idle`. A start that fails or times out makes the record `failed`, with the message in
`error`.

The CLI polls `GET /api/sessions/<session-id>` every 0.5 s, for up to 60 s, until the status leaves
`starting`. It reports a `failed` start's `error`, or any other ended status, and exits; after
60 s it reports that the session is still starting and leaves it running.

During agent startup, `SessionLog` opens the session JSONL file. If the log is empty, the agent
publishes the first event through `SessionEventBus.emit()`:

```text
session_started   persisted
```

There are no WebSocket clients yet, so the TUI normally learns this event through its initial
history read. `session_started` records the log schema version, initial model key, and session
start time.

Once the record has left `starting`, the CLI starts the TUI with two URLs:

```text
WebSocket: ws://<orchestrator>/api/sessions/<session-id>/stream
HTTP base: http://<orchestrator>/api/sessions/<session-id>
```

`archie attach` reaches the same connection flow for an already-running session.

## 5. Initial TUI connection

The connection procedure combines live subscription and a history read. The goal is to avoid
losing events produced while the log is being read.

### Step 1: open the WebSocket

The TUI connects to the orchestrator's session WebSocket. The orchestrator:

1. resolves the session ID to the running agent's port;
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
  "protocol_version":4,
  "session_id":"..."
}
```

`session_status` carries live runtime status: the active model key, the Git branch, the session
`state` (`idle`, `working` or `blocked`), and the pending `question` when the root agent is waiting
for an answer. A client that connects while a question is pending learns about it here. Neither
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
GET /api/sessions/<session-id>/events
```

For a new session, the response normally contains only `session_started`. For an existing session,
it contains the complete persisted history. Each NDJSON line is decoded through the same public
event decoder used for live events and passed to `_apply_event(event)`.

The TUI records every applied persisted event ID in a deduplication set. Its cursor advances only
when `_apply_event()` accepts a persisted event; live-only events never advance it.

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

The TUI uses one `_apply_event(event)` path for events received from both live
WebSocket delivery and HTTP history reads. Each event goes through three steps:

1. **Deduplicate.** An event ID that has already been applied is dropped, so an event received
   once through a history read and once through live delivery is applied once. A persisted
   event advances the replay cursor.
2. **Reduce.** `TranscriptBuilder.apply(event)` in `archie_shared.transcript` updates the
   transcript model and returns the `Change` records the event caused (`append`, `replace`,
   `append_text`, `remove`).
3. **Render.** The main conversation view and every open popup apply those changes to their
   widgets. Then the app runs root-only client effects: the status bar, the throbber, and
   turn state.

The orchestrator's web viewer uses the same `TranscriptBuilder`, so the event rules below apply
to both clients. There is no second reducer or separate replay schema. The model holds data only:
turns, text, streams, iteration groups, tools, subagents, compactions and notices. Each client
renders it in its own way.

### User messages

A root `user_message` starts a new turn node and resets per-turn display metrics. It also
returns the conversation view to its latest rows (see [The model and its views](#the-model-and-its-views)).

### Streamed assistant text

For a `text_delta`, the builder:

1. creates a stream node at the end of the turn, or of the subagent's transcript for child text,
   if one doesn't exist yet;
2. appends the chunk and emits an `append_text` change;
3. keys the stream by exact request identity.

The key is `(scope, subagent_index, turn, iteration, request_id)`, so sibling streams and
interleaved root and child streams can't append to each other's text. The TUI's
`StreamingMessage` writes each chunk through Textual's `MarkdownStream` rather than re-rendering
the whole text.

When an authoritative `assistant_message` arrives:

- if a stream with its exact request identity exists, the builder replaces the stream with the
  persisted text in the same position. The TUI keeps the streamed widget when the text matches,
  and re-renders it when the text differs;
- if there is no stream, as during ordinary history reads, the builder adds the persisted text
  directly;
- the builder records the identity as finalized, so later deltas for that request are ignored.

The persisted event always wins. `text_delta` exists for responsiveness, not durability. A
cumulative whole-turn request-ID list is not used; each assistant record has one direct
`request_id` and one integer `iteration`.

### Tools

`tool_call` adds a pending tool node. Tool calls are grouped into an iteration node by
`(scope, subagent_index, turn, iteration)`, one group per provider response, so parallel calls
from one response share a group. `tool_result` finds the tool by `tool_use_id`, records the
result, status and duration, and emits a `replace` change. Rows derive their summary from the
call and result. `exec` and `shell` rows show at most 10 lines of source or command.

`iteration_start` does not create a group. The builder creates an iteration group lazily, on the
first tool call (or an orphan result), so text-only iterations have no empty groups.

### Subagents

Child events, which have a non-null `scope` and `subagent_index`, belong to the `task` tool call
whose `tool_use_id` is their scope. The builder keeps each child as a subagent node under that
tool, with its own transcript items, status, activity and cost. The main view shows a child only
as a subagent row under its `task` tool. The child's transcript appears in the subagent popup.

### The model and its views

The model keeps every row of the session. The TUI's `TranscriptView` mounts only a window of
about 200 top-level rows. Scrolling to either edge pages earlier or later rows in, keeping the
row at the edge in place, and boundary rows show how many rows are hidden. Live rows are mounted
while the window shows the latest rows. While you read history, they only increase the hidden
count below.

Clicking a tool row opens a `ToolDetailScreen` with the call's full input and result. Clicking a
subagent row opens a `SubagentScreen` with the child's transcript, in its own `TranscriptView`.
Both are modal screens that read live model nodes, so they update as events arrive. A breadcrumb
line names the levels above the popup. Escape, or a click outside the popup, closes one level.

### Accounting

Each `llm_request` is folded into cumulative input, output, cache, and cost totals. Event IDs are
deduplicated so receiving the same ledger event once through a history read and once through live
delivery does not double-count it.

The TUI does not calculate prices from current model rates. It sums the immutable `cost_usd` recorded
by the agent in each request event.

### Turn and child completion

`turn_complete`, `turn_error`, or `turn_interrupted` for a root turn ends the local turn display,
removes temporary widgets, and re-enables input. A scoped terminal event updates only the matching
subagent node's status, which its row and any open popup show. Only one of these persisted terminal
events should exist for an accepted scope and turn, except when the storage append path itself has
failed.

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

On the agent-to-TUI path, it forwards frames without inspecting them. The SQLite metrics index is
built separately, by tailing the session logs (see [Architecture](architecture.md#orchestrator-metrics)),
so requests made while no client is attached are still indexed.

The metrics tables in `archie.db` are an index for aggregate queries. They are not the canonical
event store. The JSONL session log remains authoritative.

For `GET /events`, the orchestrator forwards the optional `after` cursor unchanged and relays the
agent's status code, body, and content type. Once the session has ended and its agent is gone, the
orchestrator reads the session log itself and answers in the agent's format: the same NDJSON, the
same `after` handling and the same 409 for an unknown cursor. An ended session that never wrote a
log (a `failed` start) returns an empty body.

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
5. Apply missed persisted events in log order through _apply_event(event)
6. Flush buffered live events through _apply_event(event), deduplicated by event ID and request identity
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

### Questions

The root agent's `question` tool asks the user one question and waits. The agent emits
`session_status` with `state=blocked` and the `question` (its `id` is the tool call's
`tool_use_id`). The TUI shows the question above the input and replies with one flat command:

```json
{"type":"answer","question_id":"tooluse_01","option":1}
```

`text` or `dismiss: true` replace `option` for a typed answer or a dismissal. The agent resolves
the tool call, emits `session_status` with `state=working` and no question, and the tool returns a
normal `tool_result`. An interrupt while the question waits cancels the tool call like any other.
No question event is persisted.

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
2. **The orchestrator is a relay, not the session authority.** It owns container lifecycle, the
   session records and a best-effort metrics index; the conversation lives in the agent's log.
3. **Persisted events are appended before broadcast.** A live persisted event already exists in the
   JSONL log.
4. **`text_delta` is temporary.** `assistant_message` is the durable authority for assistant text.
5. **Every assistant record has one direct request identity.** The identity is
   `(scope, subagent_index, turn, iteration, request_id)`; there is no cumulative request-ID list.
6. **The client replay cursor names only persisted events.** Never use a handshake, status, delta, or
   error-notice ID as `after=`.
7. **One `_apply_event(event)` path handles live and history events.** Event-ID and
   request-key deduplication makes overlap safe.
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
CLI ──POST /api/sessions──▶ orchestrator: record starting, 202
orchestrator ──start container (background)──▶ agent
orchestrator ──follow /stream──▶ agent; session_status → record idle
CLI ──GET /api/sessions/<id> until not starting──▶ orchestrator
agent ──SessionLog.append(session_started)──▶ JSONL

CONNECT
TUI ──WebSocket──▶ orchestrator ──WebSocket──▶ agent
agent ──handshake, session_status (live-only)──▶ TUI buffer
TUI ──GET /events──▶ orchestrator ──GET /events──▶ agent
agent ──persisted NDJSON history──▶ TUI _apply_event(event)
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
