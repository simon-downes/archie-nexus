# 009 — Model Selection via Command Palette

## Objective

Add a Textual command palette (Ctrl+P) to the TUI that lists all models from the catalog
and allows switching the active model mid-session. The switch propagates via a new
`switch_model` WebSocket command to the agent server, which rebuilds its LLM client and
confirms the change with a `model_switched` event.

## Context

The current setup selects the model at startup from `NexusConfig.global_.model` and locks
it for the session lifetime. The model catalog (`shared/src/archie_shared/models.py`) is
importable from both CLI and agent packages via `from archie_shared.models import load_models`.
Textual ≥1.0 provides a built-in `CommandPalette` triggered by Ctrl+P that integrates via a
custom `Provider` class yielding `Hit` objects.

Key existing structures:
- Wire protocol: `shared/src/archie_shared/events.py` — `_CLIENT_COMMAND_TYPES` registry,
  `deserialize_command()`, `serialize_command()`
- Agent: `AgentHarness` in `agent/src/archie_agent/harness.py` owns `_turn_active`, `_llm`,
  `session`, `_system_prompt`, `_broadcast()`
- LLM: `BedrockClient(model_id, region, max_output_tokens, can_cache)` in
  `agent/src/archie_agent/llm/bedrock.py`
- Prompt: `build_system_prompt(model_name, workspace_dir=...)` in
  `agent/src/archie_agent/prompt.py`
- Session: `Session` dataclass with `model_id: str` and `model: ModelEntry`
- TUI: `StatusBar` in `cli/src/archie_cli/tui/status.py` has reactive `model_name: str`
- Client: `WSClient` in `cli/src/archie_cli/ws_client.py`

## Requirements

- MUST add a `SwitchModelCommand` to the wire protocol (client→server, type `switch_model`)
  - AC: `deserialize_command({"type":"switch_model","data":{"model_key":"bedrock-claude-haiku-4-5"}})` returns a `SwitchModelCommand`
- MUST add a `ModelSwitched` event to the wire protocol (server→client, type `model_switched`)
  - AC: Event carries `model_key` and `model_name`; round-trips via serialize/deserialize
- MUST register both in `_CLIENT_COMMAND_TYPES` and `_SERVER_EVENT_TYPES` respectively
- MUST register a Textual `Provider` on the TUI app that yields one `Hit` per model
  - AC: Ctrl+P shows all models from `load_models()`
- MUST support fuzzy search via Textual's built-in `Matcher`
  - AC: Typing "haik" filters to matching entries
- MUST send `switch_model` command over WebSocket when a model is selected
- MUST NOT allow model switch while a turn is active (client-side guard)
  - AC: Provider action is a no-op when `_turn_active` is True
- MUST handle `SwitchModelCommand` on agent side: reject if `_turn_active`
  - AC: Broadcast `TurnError` if turn active; otherwise proceed
- MUST rebuild `BedrockClient` with the new model's provider config
  - AC: After switch, `_llm.model_id` equals the new model's endpoint
- MUST update `session.model_id` and `session.model`
- MUST rebuild system prompt via `build_system_prompt(new_model.name)`
- MUST broadcast `ModelSwitched` event after successful switch
- MUST update `StatusBar.model_name` on receiving `ModelSwitched`
- SHOULD show cost info in Hit help text
- SHOULD update `StatusBar.supports_cache` based on new model's `can_cache`
- MAY display a notification confirming the switch
- MUST NOT restart session or clear conversation history

## Technical Design

### Wire Protocol (`shared/src/archie_shared/events.py`)

```python
@dataclass(frozen=True)
class SwitchModelCommand:
    model_key: str  # catalog key

    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> SwitchModelCommand: ...

@dataclass(frozen=True)
class ModelSwitched:
    model_key: str
    model_name: str

    def to_json(self) -> dict: ...  # no turn_index (session-level)
    @classmethod
    def from_json(cls, data: dict) -> ModelSwitched: ...
```

Register in `_CLIENT_COMMAND_TYPES["switch_model"]` and `_SERVER_EVENT_TYPES["model_switched"]`.
`ModelSwitched` has no `turn_index` (like `SessionInfo`).

### Agent Handling (`agent/src/archie_agent/app.py`)

Handle `SwitchModelCommand` in `stream()`:
1. Guard: if `_agent.turn_active` → broadcast `TurnError`
2. Validate: `get_model(catalog, cmd.model_key)` — catch `KeyError` → broadcast `TurnError`
3. Rebuild: new `BedrockClient`, update session + prompt
4. Confirm: broadcast `ModelSwitched`

Promote `catalog` and `config` to module-level (alongside `_agent`) during lifespan.

### TUI Provider (`cli/src/archie_cli/tui/models_provider.py`)

`ModelProvider(Provider)` overrides `search(query) -> Hits`:
- Load catalog, iterate sorted items, apply `self.matcher(query)`, yield `Hit` per match
- Callback calls `self.app.switch_model(key)`
- Register via `COMMANDS = App.COMMANDS | {ModelProvider}` on `ArchieApp`

### WSClient Extension

Add `send_command(command)` that serializes any `ClientCommand` and sends over WS.

### TUI Event Handling

In `_handle_event()`: `ModelSwitched` → update `status.model_name`.

## Milestones

### 1. Wire protocol — SwitchModelCommand and ModelSwitched

**Approach:**
Follow the pattern of existing `MessageCommand`/`InterruptCommand` for the command and
`TurnComplete` for the event. `ModelSwitched` has no `turn_index` (handled like `SessionInfo`
in `deserialize_event()`).

**Tasks:**
- Add `SwitchModelCommand` dataclass to `events.py`
- Add `ModelSwitched` dataclass to `events.py`
- Register in type dictionaries
- Update type unions
- Handle `ModelSwitched` in `deserialize_event()` (no turn_index branch)
- Add `send_command()` to `WSClient`
- Add serialize/deserialize round-trip tests

**Deliverable:** Wire protocol supports both types with full serialization.

**Verify:** `uv run pytest tests/ -v -k "switch_model or model_switched"` passes.

---

### 2. Agent-side model switch handler

**Approach:**
Handle `SwitchModelCommand` in `stream()`. Promote `catalog`/`config` to module level.
Guard against active turn, validate model key, rebuild LLM client + session + prompt,
broadcast confirmation.

**Tasks:**
- Promote `catalog` and `config` to module-level variables in `app.py`
- Add `SwitchModelCommand` handling in `stream()`
- Implement guard, validation, rebuild, confirm logic
- Add tests: success, during-active-turn, invalid-key

**Edge Cases:**
- Unknown model_key → broadcast `TurnError` with helpful message
- Model with no explicit region → use `config.global_.region`
- Non-Bedrock provider → acceptable crash on next turn (documented)

**Deliverable:** Agent handles model switch requests correctly.

**Verify:** `uv run pytest tests/ -v -k "model_switch"` passes.

---

### 3. TUI command palette provider

**Approach:**
New `ModelProvider(Provider)` in `cli/src/archie_cli/tui/models_provider.py`. Register
on `ArchieApp`. Handle `ModelSwitched` event in `_handle_event()`.

**Tasks:**
- Create `models_provider.py` with `ModelProvider`
- Add `switch_model(key)` to `ArchieApp` (guard + send command)
- Register `COMMANDS = App.COMMANDS | {ModelProvider}`
- Handle `ModelSwitched` → update status bar
- Add unit test for `ModelProvider.search()`

**Edge Cases:**
- Turn active when selected → no-op, optionally notify
- WS send failure → caught, error shown
- Same model re-selected → harmless (agent switches anyway)

**Deliverable:** Ctrl+P shows models, selecting one switches and updates status bar.

**Verify:** `uv run ruff check && uv run pytest -q` all pass.
