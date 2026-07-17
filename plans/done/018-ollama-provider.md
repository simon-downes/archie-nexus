# Plan 018: Ollama Provider Support

## Objective

Add Ollama provider support to archie-nexus so that local LLM models (already
defined in the model catalog) can be used for agent sessions, with runtime model
switching between Bedrock and Ollama providers.

## Context

The model catalog already contains ollama entries (`ollama-qwen3-6-35b`,
`ollama-gemma4-31b`) with `provider.name = "ollama"`, but selecting one crashes
because `app.py` unconditionally creates a `BedrockClient`. The archie-nextgen
project has a working ~230-line `OllamaClient` that ports directly to nexus's
identical LLM protocol (same `StreamEvent` types, same `Generator[StreamEvent]`
return). The main adaptation is the neutral tool config format and the
ProviderConfig restructure.

The agent runs inside a Docker container — reaching the host's ollama instance
requires `host.docker.internal` DNS resolution.

## Requirements

### Functional

- MUST implement `OllamaClient` satisfying the existing `LLMClient` protocol (stream + invoke)
  - AC: Selecting an ollama model key starts a session that streams text, handles tool calls, and reports usage
- MUST support native tool calling (Ollama/OpenAI function-calling format)
  - AC: Agent can invoke tools via ollama models
- MUST translate the neutral tool config format (`{name, description, input_schema}`) to OpenAI function format
  - AC: Tool schemas are correctly passed to the ollama API
- MUST generate client-side ULIDs for tool_use_id (ollama doesn't provide them)
  - AC: Tool call/result correlation works across iterations
- MUST handle malformed tool arguments gracefully (set `input_truncated=True`)
  - AC: Weaker models that produce bad JSON don't crash the loop
- MUST report token usage from ollama's final chunk (`prompt_eval_count`, `eval_count`)
  - AC: TUI status bar shows token counts for ollama sessions
- MUST support model switching between Bedrock and Ollama providers at runtime
  - AC: `/model ollama-qwen3-6-35b` switches to ollama; `/model bedrock-claude-sonnet-4-6` switches back
- MUST replace single `ProviderConfig` with tagged union of provider-specific structs
  - AC: `ModelEntry.provider` is typed as `BedrockProvider | OllamaProvider`, msgspec discriminates on `type` tag field
- MUST use `host.docker.internal` for ollama endpoint in container
  - AC: Ollama sessions connect successfully from inside the Docker container
- MUST add `--add-host=host.docker.internal:host-gateway` to docker run command
  - AC: Works on both Docker Desktop (macOS/Windows) and Linux Docker
- MUST add `ollama` package as a hard dependency in agent `pyproject.toml`
  - AC: Package is installed in the container image
- MUST lazy-import the `ollama` package (only when an ollama model is selected)
  - AC: Bedrock-only sessions don't import ollama at startup
- MUST introduce a provider dispatch factory replacing hardcoded `BedrockClient` instantiation
  - AC: Both lifespan startup and model-switch paths use the factory

### Non-Functional

- SHOULD pass `num_ctx` option to ollama to use the full context window from catalog
  - AC: Model uses configured context size, not ollama's default 2048
- SHOULD handle `ConnectionError` when ollama is unreachable with a clear error message
  - AC: TUI shows "Ollama is not reachable at X" rather than a stack trace
- SHOULD NOT implement retry logic for ollama (local server doesn't throttle)
- SHOULD set cache fields to 0 for ollama usage (no prompt caching support)
  - AC: Cost computation works correctly (0 × rate = $0.00)
- MAY support user-defined ollama models via `~/.nexus/models.yaml` overrides
  - AC: Users can add custom model tags (already works via override mechanism + new YAML format)

## Design Decisions

1. **ProviderConfig → tagged union:** Replace single `ProviderConfig` class with:
   - `BedrockProvider(msgspec.Struct, tag="bedrock")`: `model_id` (inference profile ID), `region: str | None`
   - `OllamaProvider(msgspec.Struct, tag="ollama")`: `model_id` (ollama model tag), `endpoint: str`
   - Type alias: `ProviderConfig = BedrockProvider | OllamaProvider`
   - Discriminator: msgspec uses `type` field in serialized form (YAML/JSON)
   - Helper: `provider_name(p) -> str` returns `p.__struct_config__.tag` for logging

2. **OllamaClient location:** `agent/src/archie_agent/llm/ollama.py` — parallel to `bedrock.py`

3. **Provider factory:** `create_llm_client(model: ModelEntry, default_region: str) -> LLMClient` in
   `llm/__init__.py`. Uses `match model.provider` with lazy import of OllamaClient inside the
   OllamaProvider branch.

4. **Endpoint values:** `DEFAULT_MODELS` ollama entries use `endpoint="host.docker.internal:11434"`.
   Factory prepends `http://` when constructing OllamaClient.

5. **Docker networking:** `--add-host=host.docker.internal:host-gateway` added unconditionally to
   `docker_cmd` list in `cli.py` `start()` command. Harmless on Docker Desktop (redundant), required on Linux.

6. **Dependency:** `ollama>=0.6,<1` in `agent/pyproject.toml`. Lazy-imported: the `ollama.py` module
   imports `ollama` at its own top level, but the module itself is only imported from the factory when
   an OllamaProvider model is selected.

7. **Tool config translation:** Neutral → OpenAI function format directly:
   `{"name", "description", "input_schema"}` → `{"type": "function", "function": {"name", "description", "parameters"}}`

8. **Error handling:** `httpx.ConnectError` and `ollama.ResponseError` → `ConnectionError` with
   descriptive message. No retry logic. The loop/harness already translates `ConnectionError` → `TurnError`.

9. **BedrockClient unchanged:** Only call sites in `app.py` change (extract `model_id` from
   `BedrockProvider` struct instead of old `ProviderConfig.endpoint`).

10. **Breaking change to models.yaml:** User overrides now require `type:` in the provider block.
    Acceptable in alpha. Example:
    ```yaml
    my-custom-ollama:
      name: "My Model"
      context: 128000
      provider:
        type: ollama
        model_id: "my-model:latest"
        endpoint: "192.168.1.100:11434"
    ```

## Milestones

### 1. Refactor ProviderConfig to tagged union

Approach:
- Replace `ProviderConfig` class in `shared/src/archie_shared/models.py` with `BedrockProvider` and
  `OllamaProvider` msgspec Structs using `tag` parameter
- Add type alias `ProviderConfig = BedrockProvider | OllamaProvider` for annotation compat
- Add `provider_name(provider: ProviderConfig) -> str` helper that returns `provider.__struct_config__.tag`
- Update all `DEFAULT_MODELS` entries:
  - Bedrock: `ProviderConfig(name="bedrock", endpoint="eu.anthropic.claude-sonnet-4-6")` →
    `BedrockProvider(model_id="eu.anthropic.claude-sonnet-4-6")`
  - Ollama: `ProviderConfig(name="ollama", endpoint="localhost:11434")` →
    `OllamaProvider(model_id="qwen3.6:35b", endpoint="host.docker.internal:11434")`
    (second entry: `model_id="gemma4:31b"`)
- ⚠️ `app.py` lines ~96,250: `model.provider.endpoint` → `model.provider.model_id`
- ⚠️ `app.py` lines ~93,247: `model.provider.region` → only valid on BedrockProvider (add isinstance guard or match)
- ⚠️ `harness.py` lines 486,497: `self.session.model.provider.name` → `provider_name(self.session.model.provider)`
- ⚠️ `tests/test_model_switch.py` lines 123,131,322,328,363: constructs `ProviderConfig(name=..., endpoint=...)`
- ⚠️ `tests/test_harness.py` line 21: constructs `ProviderConfig(...)`
- ⚠️ `tests/test_native_dispatch.py` line 23: constructs `ProviderConfig(...)`
- ⚠️ `tests/test_models_catalog.py` lines 25-26,80: asserts on `model.provider.name`

Tasks:
- Replace `ProviderConfig` with `BedrockProvider` + `OllamaProvider` + type alias + helper
- Update all `DEFAULT_MODELS` entries with correct field names and values
- Update `app.py` lifespan: extract `model_id` from provider, guard `region` access
- Update `app.py` `_handle_model_switch`: same changes
- Update `harness.py`: use `provider_name()` for logging
- Fix `tests/test_model_switch.py`: replace `ProviderConfig(...)` with `BedrockProvider(...)`
- Fix `tests/test_harness.py`: replace `ProviderConfig(...)` with `BedrockProvider(...)`
- Fix `tests/test_native_dispatch.py`: replace `ProviderConfig(...)` with `BedrockProvider(...)`
- Fix `tests/test_models_catalog.py`: adapt provider type assertion to isinstance check
- Run `uv run ruff check .` and `uv run pytest tests/ -q`

Deliverable: All existing functionality works identically with the new struct hierarchy; tests pass.
Verify: `uv run pytest tests/ -q` passes, `uv run ruff check .` clean.

### 2. Implement OllamaClient

Approach:
- Port from `archie-nextgen/src/archie/llm/ollama.py` (~230 lines)
- Key differences from nextgen:
  - Import `StreamEvent` types from `archie_agent.llm._types` (not `archie.llm.bedrock`)
  - Import `Turn` from `archie_agent.session`
  - Import content block types from `archie_shared.types`
  - Tool config is neutral format `{name, description, input_schema}` (nextgen uses Bedrock's `{toolSpec: ...}`)
  - `_tool_config_to_ollama()` translates directly: `input_schema` → `parameters`
- Synchronous generator — same threading model as BedrockClient (loop calls via `asyncio.to_thread`)
- Constructor: `OllamaClient(model_id: str, host: str, max_context_tokens: int, timeout: float = 240.0)`
- `self.client = ollama.Client(host=host, timeout=httpx.Timeout(timeout))`
- Passes `options={"num_ctx": max_context_tokens}` to use full context window

Edge cases:
- Ollama unreachable (`httpx.ConnectError`): raise `ConnectionError("Ollama is not reachable at {host}")`
- Ollama API error (`ollama.ResponseError`): raise `ConnectionError("Ollama error: {msg}")`
- Malformed tool arguments (not a dict): set `input={}`, `input_truncated=True`
- Token counts None on final chunk: default to 0
- No tool_use_id from server: generate via `str(ULID())`
- Stop reason mapping: `"stop"` → `"end_turn"`, `"length"` → `"max_tokens"`, tool_calls present → `"tool_use"`
- Tool calls emitted after stream exhausted (ToolUseStart + ToolUseEvent back-to-back per call)

Tasks:
- Add `"ollama>=0.6,<1"` to `agent/pyproject.toml` dependencies
- Create `agent/src/archie_agent/llm/ollama.py` with:
  - `_turns_to_ollama_messages(turns, system)` — system as role=system message, map content blocks
  - `_tool_config_to_ollama(tool_config)` — neutral → OpenAI function format
  - `OllamaClient` class with `stream()` and `invoke()` methods
- Create `tests/test_ollama.py` with mocked `ollama.Client`:
  - Test text streaming (yields TextDelta chunks + Usage + Done)
  - Test tool call handling (yields ToolUseStart + ToolUseEvent)
  - Test malformed tool arguments (input_truncated=True)
  - Test connection error handling
  - Test invoke() returns text
- Run `uv run pytest tests/test_ollama.py -v` and `uv run ruff check .`

Deliverable: `OllamaClient` satisfies `LLMClient` protocol and handles all edge cases.
Verify: `uv run pytest tests/test_ollama.py -v` all pass; `from archie_agent.llm.ollama import OllamaClient` succeeds.

### 3. Provider dispatch factory + Docker networking

Approach:
- Add `create_llm_client(model: ModelEntry, default_region: str) -> LLMClient` to
  `agent/src/archie_agent/llm/__init__.py`
- Implementation uses `match model.provider`:
  - `case BedrockProvider(model_id=mid, region=region)`: import and return `BedrockClient(...)`
  - `case OllamaProvider(model_id=mid, endpoint=endpoint)`: lazy import `from archie_agent.llm.ollama import OllamaClient`, return `OllamaClient(model_id=mid, host=f"http://{endpoint}", max_context_tokens=model.context)`
  - `case _`: raise `ValueError(f"Unknown provider: {model.provider}")`
- Replace both `BedrockClient(...)` calls in `app.py` with `create_llm_client(model, region)`
- Remove `from archie_agent.llm.bedrock import BedrockClient` from `app.py` (import moves to factory)
- Add `"--add-host=host.docker.internal:host-gateway"` to the `docker_cmd` list in `cli.py` `start()`
  command, alongside the other docker run flags (around line ~247, after the volume mounts)

Wiring:
- State: `model: ModelEntry` (already resolved from catalog at both call sites)
- Producers: `lifespan()` creates initial client; `_handle_model_switch()` creates replacement
- Consumers: `AgentHarness` receives `LLMClient` via constructor (startup) or `switch_model()` (runtime)
- Call site: `llm_client = create_llm_client(model, region)` — single line replaces 5-line BedrockClient construction

Edge cases:
- Unknown provider type in catalog: `ValueError` raised (shouldn't happen with typed union)
- Model switch bedrock→ollama while ollama is down: `ConnectionError` raised on first `stream()` call
  (not at client creation) — loop already handles this → yields `TurnError` → TUI shows error
- Model switch ollama→bedrock: works identically to current bedrock→bedrock switch

Tasks:
- Add `create_llm_client()` to `agent/src/archie_agent/llm/__init__.py`
- Export it in `__all__`
- Update `app.py` lifespan: replace `BedrockClient(...)` with `create_llm_client(model, region)`
- Update `app.py` `_handle_model_switch`: replace `BedrockClient(...)` with `create_llm_client(new_model, region)`
- Remove `from archie_agent.llm.bedrock import BedrockClient` from `app.py`
- Add `"--add-host=host.docker.internal:host-gateway"` to docker_cmd in `cli.py`
- Add test in `tests/test_017_features.py` or new file: factory returns `BedrockClient` for BedrockProvider, `OllamaClient` for OllamaProvider
- Run full test suite: `uv run pytest tests/ -q` + `uv run ruff check .`

Deliverable: Selecting an ollama model at startup or via model switch instantiates `OllamaClient`; Docker container can resolve `host.docker.internal`.
Verify: `uv run pytest tests/ -q` all pass; manual test: set `model: ollama-qwen3-6-35b` in `~/.nexus/config.yaml`, run `archie start`, confirm agent connects to ollama and streams a response.
