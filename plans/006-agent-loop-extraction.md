# 006 — Agent Loop Extraction & Session Log Split

## Objective

Factor the agent loop into a pure, testable core function separate from I/O
concerns (WebSocket broadcast, persistence). Simultaneously switch the session
log from per-exchange entries to per-message entries for crash resilience and
natural tool-call representation. Introduce a `FakeLLMClient` for deterministic
testing without mocking boto3 internals.

## Context

The current `AgentLoop.handle_message()` (agent.py, ~130 lines) mixes:
- LLM orchestration (call provider, accumulate text, handle tool calls)
- Session state mutation (add turns, update token totals)
- WebSocket broadcasting (translate stream events → wire events, fan out)
- Thread management (spawn worker, drain queue, join)
- Persistence (build TurnLog, flush to JSONL)

This makes testing require full WebSocket mocks, makes adding features (tool
dispatch, steering messages, context management) increasingly fragile, and
couples the loop to the current sync-thread-to-async-queue bridge.

Additionally, the session log writes one line per complete exchange (user +
assistant bundled). A crash mid-response loses the user's prompt. Tool-calling
(next plan) requires representing multi-step exchanges that don't fit a single
`{user, assistant}` line.

**Tau inspiration:** pure `run_agent_loop()` async generator (no state, no I/O);
`AgentHarness` as stateful wrapper; per-message persistence with immediate flush.

## Requirements

### Pure run loop
- MUST extract a pure `run_loop()` async generator that yields `AgentEvent`s
- MUST NOT import or reference WebSocket, Starlette, or persistence in the loop
- MUST accept: messages list, system prompt, LLM client, interrupt signal
- MUST yield events for: text delta, usage, turn complete, turn error
- MUST handle the sync→async thread bridge internally (caller doesn't care)
- MUST check interrupt between yields and yield a `TurnInterrupted` event

### Agent events (internal, loop → harness boundary)
- MUST define agent-level events separate from wire protocol events
- Types: `TextChunk`, `TurnUsage`, `TurnDone`, `TurnFailed`, `TurnInterrupted`
- These are NOT the wire events — the harness translates them
- `TurnUsage` carries per-request token counts (not cumulative)

### Harness (replaces current AgentLoop)
- MUST own: session state, turn lifecycle, interrupt flag
- MUST consume loop events and: update session, persist, translate to wire events
- MUST broadcast wire events to connected clients
- MUST persist the user message immediately on receipt (before streaming)
- MUST persist the assistant message on completion (or partial on interrupt)

### Per-message session log
- MUST switch from per-exchange to per-message JSONL entries
- Each line: `{"id", "when", "role", "content", "metadata"}`
- `role`: `"user"` | `"assistant"` (later: `"tool_result"`)
- User entries: minimal metadata (just id, when, role, content)
- Assistant entries: full metadata (model, tokens, cost, interrupted, backend)
- User message persisted immediately on receipt → crash-safe
- Assistant message persisted on completion
- MUST retain backward compatibility: old `SessionLogEntry` lines in existing
  files won't break readers (different shape, but readers handle both or ignore)

### FakeLLMClient
- MUST implement `LLMClient` protocol
- MUST yield scripted `StreamEvent` sequences (configurable per test)
- MUST support delay between events (for interrupt testing)
- MUST replace the current `_make_mock_llm` / `MagicMock` pattern in tests

### FakeProvider (integration testing)
- MUST provide a fake Bedrock-like provider that doesn't need AWS credentials
- MUST be usable for `archie start` in a dev/demo mode (no real API calls)
- Implementation: a `FakeLLMClient` subclass registered as a provider, selected
  via config `global.model: fake-echo` or similar

## Technical Design

### Module layout (agent package)

```
agent/src/archie_agent/
├── app.py          (Starlette, WS endpoint, lifespan — unchanged shape)
├── harness.py      (NEW — AgentHarness, owns session + broadcast + persistence)
├── loop.py         (NEW — pure run_loop async generator)
├── events.py       (NEW — agent-level event types)
├── session.py      (REWRITTEN — in-memory transcript only, no persistence)
├── llm/
│   ├── __init__.py (LLMClient protocol, unchanged)
│   ├── _types.py   (StreamEvent types, unchanged)
│   ├── bedrock.py  (unchanged)
│   └── fake.py     (NEW — FakeLLMClient)
```

### Agent events (`agent/events.py`)

```python
@dataclass
class TextChunk:
    text: str

@dataclass
class TurnUsage:
    """Per-request usage (not cumulative)."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

@dataclass
class TurnDone:
    stop_reason: str

@dataclass
class TurnFailed:
    error: str

@dataclass
class TurnInterrupted:
    pass

type AgentEvent = TextChunk | TurnUsage | TurnDone | TurnFailed | TurnInterrupted
```

### Pure loop (`agent/loop.py`)

```python
async def run_loop(
    *,
    llm: LLMClient,
    messages: list[Turn],
    system: str,
    interrupt: threading.Event,
) -> AsyncGenerator[AgentEvent]:
    """Run one LLM call, yielding agent events.

    Spawns the sync LLM generator in a thread, bridges to async via queue.
    Checks interrupt between yields. Caller owns all state.
    """
```

The loop does ONE thing: call the LLM, yield events. No session mutation, no
broadcasting, no persistence. The thread/queue bridge lives here (it's an
implementation detail of calling a sync generator from async code).

### Harness (`agent/harness.py`)

```python
class AgentHarness:
    """Stateful agent brain — owns session, translates events, broadcasts."""

    def __init__(self, session, llm, model, system_prompt):
        ...

    async def handle_message(self, content: str) -> None:
        """Process user message: persist, run loop, persist result, broadcast."""
        # 1. Persist user message immediately
        # 2. Add to in-memory transcript
        # 3. Run loop, consuming AgentEvents:
        #    - TextChunk → accumulate + broadcast TextDeltaEvent
        #    - TurnUsage → update session totals + broadcast UsageUpdated
        #    - TurnDone → persist assistant message + broadcast TurnComplete
        #    - TurnFailed → broadcast TurnError
        #    - TurnInterrupted → persist partial + broadcast TurnInterrupted
```

### Session log format change (`shared/session/log.py`)

Replace `SessionLogEntry` with:

```python
class MessageEntry(msgspec.Struct):
    """One JSONL line — a single message in the conversation."""
    id: str                         # ULID
    when: str                       # ISO-8601 UTC
    role: str                       # "user" | "assistant" | "tool_result" (future)
    content: str
    metadata: MessageMetadata | None = None  # None for user messages

class MessageMetadata(msgspec.Struct):
    """Metadata attached to assistant messages."""
    model: str
    backend: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False
```

`write_entry` stays the same (append one line). Called twice per exchange:
once for user message, once for assistant message.

### Session (`agent/session.py`) — trimmed

Remove persistence concerns. Session becomes pure in-memory transcript:
- `turns: list[Turn]` for LLM context building
- Token accumulators for lifetime totals
- `context_pct`, `context_warning` properties
- `add_turn()` — memory only
- NO `flush_turn()`, NO `TurnLog`, NO `log_path`, NO `_log_dir`

Persistence moves to the harness (which calls `write_entry` directly).

### FakeLLMClient (`agent/llm/fake.py`)

```python
class FakeLLMClient:
    """Deterministic LLM client for testing. Yields scripted events."""

    def __init__(self, responses: list[list[StreamEvent]], delay: float = 0):
        self.model_id = "fake-echo"
        self._responses = responses
        self._call_index = 0
        self._delay = delay

    def stream(self, messages, system, tool_config=None):
        events = self._responses[self._call_index]
        self._call_index += 1
        for event in events:
            if self._delay:
                time.sleep(self._delay)
            yield event

    def invoke(self, messages, system):
        return "fake response"
```

Tests become: construct `FakeLLMClient` with expected events → run harness →
assert broadcast events. No `MagicMock`, no `patch("boto3")`.

---

## Milestones

### 1. Agent events + pure loop

**Approach:**
- Create `agent/src/archie_agent/events.py` with the five event types.
- Create `agent/src/archie_agent/loop.py` with `run_loop()` — extract the
  thread spawn + queue drain + interrupt check from `handle_message()`.
- `run_loop()` yields `AgentEvent`s, NOT `StreamEvent`s — it translates
  `TextDelta→TextChunk`, `Usage→TurnUsage`, `Done→TurnDone`, error→`TurnFailed`,
  interrupt→`TurnInterrupted`.

**Tasks:**
- Implement `events.py`
- Implement `loop.py`
- Unit test `loop.py` with a simple inline fake (list of StreamEvents)

**Deliverable:** Pure loop, testable in isolation.

**Verify:** `uv run pytest tests/test_loop.py -v`

---

### 2. FakeLLMClient

**Approach:**
- Create `agent/src/archie_agent/llm/fake.py`.
- Configurable: list of responses (each is a list of `StreamEvent`s),
  optional delay between events.
- Update `llm/__init__.py` exports.

**Tasks:**
- Implement `FakeLLMClient`
- Write tests proving it works with `run_loop()`

**Deliverable:** Drop-in test client, no mocks needed.

**Verify:** `uv run pytest tests/test_fake_llm.py -v`

---

### 3. Per-message session log

**Approach:**
- Replace `SessionLogEntry` + `EntryMetadata` with `MessageEntry` +
  `MessageMetadata` in `shared/session/log.py`.
- `write_entry` unchanged (append one JSONL line).
- Update `session/__init__.py` exports.
- Update tests.

**Edge cases:**
- Old log files with `SessionLogEntry` format: not a concern (write-only,
  no reader exists). New sessions get new format; old files are inert.

**Tasks:**
- Rewrite `log.py` structs
- Update `shared/session/__init__.py`
- Rewrite `tests/test_session_log.py`

**Deliverable:** Per-message schema ready for harness to use.

**Verify:** `uv run pytest tests/test_session_log.py -v`

---

### 4. Harness + session trim

**Approach:**
- Create `agent/src/archie_agent/harness.py` with `AgentHarness`.
- Trim `session.py`: remove `flush_turn`, `TurnLog`, `log_path`, `_log_dir`.
  Keep: `Turn`, `Session` (in-memory transcript + token accumulators).
- Harness consumes `run_loop()` events, updates session, persists messages
  (user immediately, assistant on completion), broadcasts wire events.
- `app.py` lifespan creates `AgentHarness` instead of `AgentLoop`.

**Tasks:**
- Implement `harness.py`
- Trim `session.py`
- Update `app.py` to use harness
- Delete `agent.py` (fully replaced by harness + loop)
- Fix all tests (use `FakeLLMClient` + harness instead of old `AgentLoop`)

**Deliverable:** Clean split: loop (pure) → harness (state + I/O) → app (HTTP/WS).

**Verify:** `uv run ruff check && uv run pytest -v` — all pass.

---

### 5. Test migration

**Approach:**
- Rewrite `test_agent_loop.py` → `test_harness.py` using `FakeLLMClient`.
  No more `MagicMock`, no `patch("boto3")`. Tests exercise the full path:
  FakeLLM → loop → harness → wire events (captured via fake WS).
- Rewrite `test_bedrock.py` to only test BedrockClient in isolation (stream
  parsing, retry logic). These still mock boto3 (testing the provider adapter).
- Update `test_ws_integration.py` to use the new harness + fake.

**Tasks:**
- Rewrite test files
- Ensure coverage: normal flow, interrupt, error, usage/cost propagation

**Deliverable:** Test suite uses fakes, not mocks. Faster, more readable, more reliable.

**Verify:** `uv run ruff check && uv run pytest -v` — all pass.
