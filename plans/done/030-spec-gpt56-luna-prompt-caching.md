## Objective

Update GPT-5.6 Luna's Bedrock Responses adapter to use a typed developer prompt and simple advancing prompt-cache boundaries. Preserve the existing neutral conversation transcript, tool loop, events, Converse/Ollama behavior, and persisted logging while making emitted usage fields represent billable token categories consistently across providers.

## Context

GPT-5.6 Luna is exposed through Amazon Bedrock's OpenAI-compatible Responses API on the `bedrock-mantle` endpoint. Existing Bedrock models use the separate Converse API and its `cachePoint` request format, so Luna is implemented by `BedrockOpenAIClient`.

The current Luna adapter sends the assembled system prompt through the Responses API `instructions` parameter and does not send explicit cache parameters. GPT-5.6 explicit caching instead places `prompt_cache_breakpoint` on typed input content blocks. The current prompt builder returns one monolithic string even though it already assembles identity, environment, tools, skills, project context, and loaded skill bodies as distinct conceptual sections.

The requested prompt layout is:

```text
static system prompt
  identity + environment + tool guidance
  constant skill catalog
  session-snapshot AGENTS.md
  [cache point 1]

dynamic system prompt
  loaded skill bodies and future dynamic content
  [cache point 2]

conversation input
  cache point 3 advances to the latest eligible input_text block

new suffix
```

The conversation is append-only and has no rewind operation. Therefore, the conversation cache boundary should advance on every provider request. During a tool loop it advances after the latest tool-result input block; on a later user turn it advances after the new user input so that input becomes reusable on the following request. If required for incremental writes, the previous conversation marker is retained as a read-only marker while the new marker is added, staying within the four-marker request limit.

The skill catalog is detected at session start and must remain constant. `AGENTS.md` is also treated as constant for the session and is read once when the harness is created. Loaded skill bodies are the first dynamic system content.

Usage needs a billable contract. Converse reports uncached input separately from cache-read and cache-write tokens. Responses reports total input and embeds the cache portions inside its input details. Archie will emit `input_tokens` as uncached input for both providers, with cache-read, cache-write, and output tokens as separate billable fields. Total context input remains a derived value for context-window reporting only.

Relevant code:

- `agent/src/archie_agent/prompt.py`
- `agent/src/archie_agent/harness.py`
- `agent/src/archie_agent/loop.py`
- `agent/src/archie_agent/llm/__init__.py`
- `agent/src/archie_agent/llm/_types.py`
- `agent/src/archie_agent/llm/bedrock.py`
- `agent/src/archie_agent/llm/bedrock_openai.py`
- `agent/src/archie_agent/llm/ollama.py`
- `shared/src/archie_shared/models.py`
- `orchestrator/src/archie_orchestrator/metrics.py`
- `cli/src/archie_cli/tui/app.py`

External API facts are taken from the [AWS GPT-5.6 prompt-caching guide](https://aws.amazon.com/blogs/machine-learning/introducing-explicit-prompt-caching-for-openai-gpt-5-6-models-on-amazon-bedrock/) and the [OpenAI prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching/).

## Requirements

### Structured prompt sections

- MUST expose the existing prompt as a provider-neutral structured prompt with exactly two logical sections: `static system prompt` and `dynamic system prompt`.
  - AC: Static content contains identity, environment, tool guidance, the constant skills catalog, and the session-snapshot `AGENTS.md` context.
  - AC: Dynamic content contains loaded skill bodies and future dynamic prompt additions.
- MUST remove mutable `[loaded]` markers from the skills catalog.
  - AC: Catalog text is identical before and after loading a skill; loaded bodies are the only skill-state-dependent skill content.
- MUST snapshot `AGENTS.md` once when the session harness is created.
  - AC: Editing `AGENTS.md` after harness creation does not change that session's prompt; a new session reads the new file.
- MUST keep the active model name out of the cacheable prompt text.
  - AC: Building prompts for two model names with the same static/dynamic inputs produces identical cacheable prompt text.
- MUST retain `build_system_prompt()` as a flattened compatibility API and add a structured builder used by the harness.
  - AC: Existing string callers receive a non-empty prompt with the existing semantic ordering.
- MUST build one prompt snapshot at the start of each outer `handle_message()` call and reuse it for every tool-loop request in that call.
  - AC: Tool execution does not rebuild the prompt during the same outer turn.

### Responses request shape

- MUST represent the Luna system prompt as one Responses API `developer` input message.
  - AC: A cache-enabled request's first input item has `type: "message"` and `role: "developer"`.
- MUST represent static system content as one typed `input_text` block and dynamic system content as one typed `input_text` block when non-empty.
  - AC: Identity/environment/tools/catalog/AGENTS are one static block; loaded skills/future dynamic content is one dynamic block.
- MUST NOT send the same prompt through both `instructions` and the developer message.
  - AC: `responses.create()` requests contain no `instructions` field.
- MUST keep tool definitions in the existing Responses `tools` field.
  - AC: Tool names, descriptions, schemas, and ordering are preserved.
- MUST use the same developer-message representation for `stream()` and `invoke()`.
  - AC: Equivalent stream and invoke calls produce equivalent prompt input shapes.
- MUST preserve existing Converse and Ollama prompt representations.
  - AC: Converse still receives a system field and Ollama still receives one system message.

### Advancing cache boundaries

- MUST place the first cache breakpoint after the static system block when caching is enabled.
  - AC: The static block contains `prompt_cache_breakpoint: {"mode": "explicit"}`.
- MUST place the second cache breakpoint after the dynamic system block when non-empty and caching is enabled.
  - AC: The dynamic block contains the second breakpoint; empty dynamic content creates no block or breakpoint.
- MUST advance one logical conversation breakpoint to the latest eligible `input_text` block in each request.
  - AC: A normal request marks the current user text for reuse on the next request; a tool-loop request marks the final tool-result text block in the batch.
- MUST preserve the prior conversation breakpoint long enough for the new breakpoint to extend an existing cache prefix when the API requires prior markers for read matching.
  - AC: During a transition, static + dynamic + previous conversation + new conversation markers never exceed four total markers.
- MUST never attach a marker to a `function_call` item.
  - AC: Function-call items retain their existing shape without cache metadata.
- MUST represent a tool result as typed `input_text` content when placing the conversation breakpoint after it.
  - AC: The final `function_call_output` in a tool-result batch can carry a marker on its `input_text` output block.
- MUST not issue an additional model request solely to advance the cache after each individual tool execution.
  - AC: Existing batching of tool results is unchanged; one marker is placed after the final result in the batch.
- MUST emit no more than four cache breakpoints in one request.
  - AC: Static, dynamic, and conversation markers remain within the service limit.
- MUST NOT store cache metadata in `Session.turns`, persisted logs, or public events.
  - AC: Existing transcript, event, and logging assertions remain unchanged.

### Cache key and options

- MUST send a deterministic `prompt_cache_key` when caching is enabled.
  - AC: Equivalent static prompt/tool inputs produce the same key across requests and processes.
- MUST derive the key from a cache schema version, canonical static prompt content, and canonical tool definitions.
  - AC: Changing static prompt, tool definitions, or schema version changes the key; changing model name, loaded skills, `AGENTS.md`, or conversation does not.
- MUST exclude model ID from the application key while recognizing that Bedrock service caches remain model-specific.
  - AC: Switching models with otherwise identical prompt/tool inputs produces the same application key and breakpoint layout, but the request still uses the selected model.
- MUST use canonical JSON with sorted object keys, compact separators, preserved tool-list order, and SHA-256 shortened into a key such as `archie:luna:v1:<digest>`.
  - AC: Key tests cover equivalent dictionary ordering and meaningful tool-list ordering.
- MUST send Bedrock prompt-cache options through `extra_body` with explicit mode and a `30m` TTL.
  - AC: Cache-enabled requests contain `extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}`.
- MUST omit cache metadata when caching is disabled.
  - AC: Non-cacheable requests contain no cache key, options, or breakpoints.
- MUST retry once without cache metadata when `responses.create()` raises a 400-level cache validation error, then disable caching for that client instance.
  - AC: The retry removes cache key, options, and breakpoint fields; unrelated errors are not retried.

### Provider-neutral billable usage

- MUST define emitted internal and wire `Usage.input_tokens` as uncached input tokens.
  - AC: `input_tokens` means the normal-rate input portion for every provider.
- MUST retain separate cache-read, cache-write, and output fields.
  - AC: Every usage event exposes `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `output_tokens` as billable categories.
- MUST map Converse usage directly from `inputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`, and `outputTokens`.
  - AC: Converse's `inputTokens` becomes Archie `input_tokens` without adding cache fields.
- MUST map Responses usage by subtracting cache portions from raw total input.
  - AC: `input_tokens = raw input_tokens - cached_tokens - cache_write_tokens`.
  - AC: `cached_tokens` maps to `cache_read_tokens`, `cache_write_tokens` maps to `cache_write_tokens`, and raw output maps to `output_tokens`.
- MUST map Ollama input as uncached input with zero cache fields.
  - AC: Existing Ollama input and output counts remain unchanged.
- MUST derive total context input separately as `input_tokens + cache_read_tokens + cache_write_tokens`.
  - AC: Context percentage uses total context input even though billable input uses only uncached input.
- MUST sanitize invalid or contradictory usage deterministically.
  - AC: Negative/non-integer values become zero; if cache fields exceed raw total input, cache fields are zeroed and uncached input is the raw total.

### Cost accounting

- MUST charge emitted `input_tokens` at the normal input rate.
  - AC: Responses normalized input is charged once at the normal input rate.
- MUST charge cache-read tokens at the cache-read rate, cache-write tokens at the cache-write rate, and output tokens at the output rate.
  - AC: Total input 135, cache read 30, cache write 5, and output 20 are priced as 100 normal input, 30 cache read, 5 cache write, and 20 output.
- MUST update `shared/src/archie_shared/models.py` and `orchestrator/src/archie_orchestrator/metrics.py` to use the billable usage contract.
  - AC: The orchestrator does not add cache rates to Responses' raw total input.
- MUST preserve existing token field names and database schema.
  - AC: Existing metrics rows remain readable without migration.
- MUST keep session context tracking based on derived total context input.
  - AC: Context percentage is not calculated from uncached input alone.

### Compatibility and validation

- MUST preserve existing Converse cache-point placement and wire format.
  - AC: Converse request-shape tests continue to show system and history-tail `cachePoint` blocks.
- MUST preserve Ollama's single system-message representation and zero cache fields.
  - AC: Existing Ollama request and usage tests continue to pass.
- MUST add request-shape regression tests for changed provider interfaces and test doubles.
  - AC: Full repository tests pass without unupdated mock signatures.
- MUST validate the actual Bedrock request shape through normal manual use after implementation.
  - AC: Any cache-shape, cache-counter, or tool-result placement issue discovered during use is fixed and covered by a local regression test.
- MUST leave unrelated pre-existing repository formatting failures untouched.
  - AC: Changed-file validation and `git diff --check` pass.

## Technical Design

### Overview

Use a structured prompt object as the internal transport between the harness and LLM providers. Existing providers flatten it back to their existing wire format; the Responses provider renders one developer message containing one static block and one optional dynamic block.

Use one logical conversation breakpoint that advances to the last eligible `input_text` in each request. During a transition, retain the previous conversation marker as a read-only match and add the new marker when needed, staying within four markers. Normalize provider usage into the four billable categories: uncached input, cache reads, cache writes, and output.

**Done when:** A fresh implementor can describe the change as “two developer blocks, one advancing conversation marker, and one billable usage contract.”

### Technical Stack

- Reuse Python dataclasses and existing agent modules; add no new runtime dependency.
- Reuse the pinned `openai` SDK (`>=2.45,<3`) and its `responses.create()` interface.
- Use the existing `extra_body` mechanism for Bedrock-specific `prompt_cache_options`.
- Use the existing `pytest`/`pytest-asyncio` setup for deterministic local tests.
- Use SHA-256 from the Python standard library for cache keys.

**Done when:** Every technology choice is named, existing versus new is clear, and no implementor must select a dependency or request mechanism.

### Architecture

- `AgentHarness` owns the session-constant `AGENTS.md` snapshot, constant skill catalog, loaded-skill state, and structured prompt snapshots.
- `run_loop()` owns current request history and forwards the latest eligible input boundary to provider clients.
- `BedrockOpenAIClient` owns Responses developer-message serialization, advancing cache markers, cache keys/options, fallback, and raw usage normalization.
- `BedrockClient` and `OllamaClient` flatten the structured prompt into their existing request formats.
- `archie_shared.models` owns billable cost calculation.
- The orchestrator metrics writer consumes billable usage fields and derives no provider-specific interpretation.

**Done when:** Each component boundary and data path from harness to provider to ledger is explicit.

### Components

**Session prompt context**

- Responsibility: hold the constant catalog and one `AGENTS.md` snapshot for the session.
- Interface: harness-owned values passed into structured prompt construction.
- State: constant catalog/project context plus mutable loaded skill bodies.

**Structured prompt builder**

- Responsibility: produce one static and one dynamic `SystemPrompt` section.
- Interface: `build_system_prompt_structured(...) -> SystemPrompt` and compatibility `build_system_prompt(...) -> str`.
- State: immutable per outer request; no persistence.

**LLM protocol and loop**

- Responsibility: carry prompt snapshots and current request input-boundary information without changing neutral turns or agent events.
- Interface: `stream(messages, system, tool_config=None, history_boundary=None)`.
- State: temporary `working_messages` remains owned by `run_loop()`.

**BedrockOpenAIClient**

- Responsibility: create Responses requests, mark static/dynamic/history input blocks, preserve the prior marker during advancement, handle fallback, and normalize Responses usage.
- Interface: existing `stream()` and `invoke()` plus optional history-boundary metadata.
- State: cache-enabled flag and private previous conversation-marker identity.

**Shared accounting**

- Responsibility: price four billable token categories and provide derived total-context input to session tracking.
- Interface: existing `calculate_cost(...)` signature and metrics fields.
- State: no new persistent state.

**Done when:** Every component states responsibility, interface, and owned state without leaving an implementor to invent boundaries.

### Data Model

**SystemPrompt**

- Required `static_system: PromptSection`.
- Optional `dynamic_system: PromptSection | None`; omitted from the Responses request when empty.
- Rendered text preserves existing prompt order after the deliberate removal of mutable loaded markers and active model text.
- Lifetime: one outer user request.

**Session prompt context**

- Required constant skill catalog text without `[loaded]` markers.
- Required session-constant `agents_context: str` captured at harness construction.
- Mutable loaded skill bodies read when each outer prompt snapshot is built.
- Lifetime: session for constant fields; outer request for rendered prompt.

**Billable Usage**

- `input_tokens: int`: uncached input tokens.
- `cache_read_tokens: int`: cache-read input tokens.
- `cache_write_tokens: int`: cache-write input tokens.
- `output_tokens: int`: output tokens.
- Derived `total_context_tokens = input_tokens + cache_read_tokens + cache_write_tokens`.
- Existing event and database field names remain unchanged.

**Cache key payload**

- Required schema version, rendered static-system text, and canonical ordered tool list.
- Model ID, dynamic context, `AGENTS.md`, loaded skills, and conversation are excluded.
- Stored only as an outbound request key; no database persistence.

**Conversation marker state**

- Private provider-client state identifies the last marked input block by a deterministic serialized-content fingerprint.
- It is used only to retain the previous conversation marker while adding the next marker.
- It is discarded when the client is replaced, such as after a model switch.

**Done when:** Every new field has a type, required/optional status, owner, and lifecycle.

### Data Flow

**Session prompt construction**

1. Harness discovers the skill catalog and captures `AGENTS.md` once at construction.
2. At the start of each user request, harness builds a static block and a dynamic block.
3. Static content includes catalog/project context; dynamic content includes current loaded skill bodies.
4. Harness passes the prompt snapshot to `run_loop()`.

**Normal user request**

1. Responses client emits static and dynamic blocks with their markers.
2. Existing conversation history is serialized.
3. The current user `input_text` is marked as the advancing conversation point for the next request.
4. The request is sent with the current user input after the static/dynamic prefix.
5. On the next request, the prior marker can be read and a new marker is added after the new input.

**Tool-loop request**

1. Model returns one or more `function_call` items.
2. Archie executes the calls and batches their results as it does today.
3. Each `function_call_output` result is serialized with typed `input_text` content when eligible for the conversation marker.
4. The final result block in the batch receives the advancing marker.
5. The next model request reads the previous prefix and processes only the appended suffix.

**Cache-marker advancement**

1. Serialize the request without new marker metadata.
2. Reapply the previous marker to its matching historical input block when present.
3. Mark the latest eligible input-text block as the new conversation boundary.
4. If both old and new markers are present, ensure static + dynamic + old + new is at most four.
5. Store the new block fingerprint for the next request.

**Usage accounting**

1. Converse adapter emits its raw uncached/cache-read/cache-write/output values directly.
2. Responses adapter subtracts `cached_tokens` and `cache_write_tokens` from raw total input.
3. Agent emits billable usage fields.
4. Session derives total context tokens for context percentage.
5. TUI and orchestrator price the same four billable categories.

**Done when:** Happy paths, tool-loop advancement, marker retention, and usage normalization are explicit from producer through consumer.

### Error Handling & Edge Cases

- Cache validation error from `responses.create()` → remove cache metadata, retry once, disable caching for the client instance.
- Non-cache Responses API error → propagate; do not retry as a cache fallback.
- Stream iteration error after request creation → propagate; do not replay the request.
- Empty dynamic context → omit its input block and breakpoint.
- No eligible input-text block after static/dynamic content → omit the conversation marker.
- Current request has no changing suffix, such as a one-shot empty-input call → do not create a conversation marker.
- Function call item → preserve shape and never attach a marker.
- Tool result is a string-only function output and cannot be converted to typed input text → preserve tool result and omit the conversation marker for that request.
- Missing Responses usage → emit zero billable usage as today.
- Negative/non-integer usage → sanitize to zero.
- Responses raw total is less than reported cache portions → zero cache portions and treat raw total as uncached input.
- `AGENTS.md` absent/unreadable at session construction → store empty context and do not reread it.
- No next request after a cache write → cache-write cost is still reported; no read is assumed.

**Done when:** An implementor can determine the response for each named failure and boundary case without inventing policy.

### External Integrations

**Amazon Bedrock `bedrock-mantle` Responses API**

- Purpose: serve GPT-5.6 Luna responses and prompt caching.
- Pattern: synchronous request returning a typed streaming response or non-streaming response.
- Authentication: existing `aws_bedrock_token_generator` provider and Archie credential fallback.
- Constraints: typed input blocks for breakpoints, 1,024-token cache-prefix minimum, maximum four cache markers per request, 30-minute cache TTL.
- Retry: one retry for a recognized cache-validation 400; no retry for unrelated API or stream errors.
- Validation: tool-output breakpoint placement must be verified by actual `cache_write_tokens`/`cached_tokens`, not just SDK type acceptance.

**Existing Bedrock Converse API**

- Purpose: serve non-Responses Bedrock models.
- Pattern: unchanged `converse_stream` and `converse` calls.
- Constraint: existing `cachePoint` system/history placement remains unchanged.
- Failure handling: retain existing cache rejection and credential-refresh behavior.

**Manual verification**

- Purpose: verify service-side cache acceptance and usage counters during normal use after implementation.
- Pattern: use Luna interactively with representative prompts and tool loops; inspect logged usage counters.
- Credentials: existing Archie Bedrock credentials/default AWS chain.
- Failure handling: fix any request-shape or accounting issue found during use and add a local regression test.

**Done when:** Each external integration names its purpose, request pattern, constraints, authentication, and failure behavior.

### Code Structure

- Extend `agent/src/archie_agent/prompt.py` with `PromptSection`, `SystemPrompt`, structured building, constant catalog rendering, and session-snapshot-compatible context inputs.
- Extend `agent/src/archie_agent/harness.py` to capture `AGENTS.md` once and build structured prompt snapshots.
- Extend `agent/src/archie_agent/loop.py` and `agent/src/archie_agent/llm/__init__.py` with structured prompt and advancing-boundary plumbing.
- Update `agent/src/archie_agent/llm/bedrock.py`, `ollama.py`, and `fake.py` to accept structured prompts and flatten/ignore metadata as appropriate.
- Update `agent/src/archie_agent/llm/bedrock_openai.py` with developer-message construction, typed tool-result output, marker advancement/retention, cache keys/options, fallback, and Responses usage normalization.
- Update `shared/src/archie_shared/models.py` with billable-category cost calculation and usage sanitization helper if needed.
- Update `agent/src/archie_agent/session.py` or the agent usage boundary so context percentage uses derived total context tokens while emitted `input_tokens` remains uncached input.
- Update `orchestrator/src/archie_orchestrator/metrics.py` to use the billable usage contract.
- Update tests in `tests/test_prompt.py`, `tests/test_bedrock.py`, `tests/test_bedrock_openai.py`, `tests/test_loop.py`, `tests/test_harness.py`, `tests/test_orchestrator_metrics.py`, and related accounting tests.
- Add local regression tests under `tests/` for request shapes, usage counters, and any cache behavior issues found during manual use.

**Done when:** Every changed or new module has a concrete path and an existing pattern to follow.

### Patterns and Conventions

- Follow existing dataclass and typed-structure patterns in agent modules.
- Keep provider-specific wire conversion inside provider clients; do not put Responses fields in `Session` or neutral `Turn` objects.
- Keep stream events provider-neutral and reuse existing text/tool/usage/done events.
- Use the existing module logger for cache fallback, session-snapshot failures, marker mismatches, and malformed-usage warnings.
- Use mocked `responses.create()` kwargs for deterministic request-shape tests; verify real Bedrock behavior manually after implementation.
- Keep test helpers deterministic and use synthetic text for local tests.
- Do not issue one model request per tool call merely to advance caching; preserve the existing batched tool-loop behavior.

**Done when:** An implementor can follow existing code and testing conventions without selecting a new pattern.

### Infrastructure and Deployment

- No new runtime infrastructure.
- No new production secrets or environment variables.
- Existing Bedrock credentials and region configuration are reused.
- No database migration; existing metrics columns remain valid.

**Done when:** New resources, environment variables, secrets, and migrations are explicitly named or explicitly ruled out.

### Non-Functional Concerns

- Reliability: cache validation falls back once to an uncached request and disables caching for the client instance; unrelated failures remain visible.
- Cost correctness: emitted fields are exactly the billable categories; one later read is not assumed unless usage reports it.
- Observability: cache fallback, marker mismatches, and malformed usage emit warnings; real-use verification inspects cache counters after implementation.
- Data handling: cache keys contain only a digest of static prompt/tool inputs; local tests use synthetic content.
- Compatibility: Converse/Ollama are verified at their existing wire boundaries, not only through shared unit types.
- Session consistency: catalog and `AGENTS.md` are immutable after harness creation; changes take effect in a new session.

**Done when:** Each relevant concern names a concrete mechanism and observable signal.

### Key Decisions

- Use one typed static block and one typed dynamic block rather than separate identity, environment, tools, and catalog blocks; they share semantics and one boundary is simpler.
- Put the constant skill catalog and session-snapshot `AGENTS.md` in the static block; loaded skills and future additions go in the dynamic block.
- Remove `[loaded]` markers rather than changing catalog text when skills load.
- Remove the active model name from cacheable prompt text and exclude model ID from the application key; model changes still use model-specific service caches and may cold-start.
- Use one logical advancing conversation boundary rather than a fixed history point or a rotating multi-turn window.
- Retain the previous conversation marker during an advancement transition only when needed to obtain an incremental cache write, subject to the four-marker limit.
- Mark the final typed tool-result block in a batched tool loop rather than issuing extra model requests after individual tool calls.
- Emit `input_tokens` as uncached input, matching Converse and the billable view; derive total context input separately.
- Keep real Bedrock verification manual rather than adding network-dependent tests; normal local tests must remain deterministic and offline.

**Done when:** Each decision names the chosen option, a rejected alternative, and a rationale tied to this codebase.

### Risks and Open Questions

**Risks**

- Bedrock may accept typed `input_text` inside `function_call_output` but not create a cache entry. Mitigation: verify during normal use and add a local regression test or omit the marker if unsupported.
- A missing prior marker causes a full extension write rather than an incremental write. Mitigation: preserve the prior marker by content fingerprint and test `cache_write_tokens` across three requests.
- A model switch creates a cold cache even with the same application key because service caches are model-specific. This is expected.
- A loaded skill or future dynamic section may be large enough to make cache writes uneconomical. Mitigation: monitor emitted read/write counters.
- Existing malformed usage fixtures may expose hidden accounting assumptions. Mitigation: update fixtures to valid billable categories and add explicit malformed-input tests.

**Open questions**

- None for the implementation plan. Assistant-output cache placement remains intentionally excluded; the advancing point uses the latest eligible typed input block, including typed tool output when service validation confirms it.

**Done when:** Every risk has mitigation or an explicit acceptance rationale, and there are no unresolved implementation questions.

## Milestones

### 1. Structured prompt and session-constant context (prefactor)

**Approach**

Use existing prompt fragment functions in `agent/src/archie_agent/prompt.py`. Add two logical sections without changing the existing semantic order. The constant skills catalog and one `AGENTS.md` snapshot are captured at harness construction; loaded skill bodies are rendered in the dynamic section per outer request. Removing `[loaded]` markers and the active model line are deliberate cache-stability changes.

Test seam: public prompt builders and the LLM client call boundary.

**Wiring**

- State: `agents_context` and constant catalog captured in `AgentHarness.__init__`; one immutable `SystemPrompt` snapshot per outer request.
- Producers: harness construction reads project rules/catalog; structured builder combines them with static fragments and loaded skill bodies.
- Consumers: `run_loop()` forwards the prompt; Converse/Ollama flatten it; Responses consumes its two blocks.
- Call site: `run_loop(messages=self.session.turns, system=self._build_prompt(), ...)` passes a `SystemPrompt | str`.

**Edge cases**

- Missing/unreadable `AGENTS.md` → store empty context and do not reread it.
- Empty loaded-skill list → omit the dynamic block.
- Skill load → catalog remains identical; only dynamic bodies change.
- Model change → cacheable prompt text remains identical when other inputs match.

**Tasks**

- Add `PromptSection` and `SystemPrompt`.
- Add `build_system_prompt_structured()` and retain flattened `build_system_prompt()`.
- Capture `AGENTS.md` once in the harness.
- Remove `[loaded]` catalog markers and active model text from cacheable prompt content.
- Update harness, loop, LLM protocol, Converse, Ollama, fake client, and repository test doubles.
- Add prompt stability, snapshot, and flattened-output tests.

**Deliverable:** The harness passes a stable two-section prompt through the provider-neutral LLM boundary.

**Verify:** Run prompt/loop/harness/provider tests and compare prompt sections before and after skill loads, `AGENTS.md` edits, and model changes.

### 2. Luna developer prompt and static/dynamic cache boundaries

**Approach**

Implement the Responses request shape in `agent/src/archie_agent/llm/bedrock_openai.py`. Use one developer message with one typed block per logical section, the pinned OpenAI SDK, Bedrock `extra_body` options, and a deterministic key from static prompt/tools.

Test seam: mocked `responses.create()` kwargs and public `stream()`/`invoke()` behavior.

**Wiring**

- State: client-level `_cache_enabled` flag and deterministic static key generated per request.
- Producers: static prompt/tool config provide key/input material; dynamic content supplies only the second block.
- Consumers: `responses.create()` receives developer input, tools, key, options, and static/dynamic markers.
- Call site: `client.stream(messages, system=prompt, tool_config=tools, history_boundary=...)` and equivalent `invoke()`.

**Edge cases**

- Empty dynamic block → only static block/marker.
- Caching disabled → no cache metadata.
- Cache validation 400 → retry once without cache metadata and disable future cache metadata.
- Unrelated API failure → propagate without retry.

**Tasks**

- Replace `instructions` with developer input serialization in `stream()` and `invoke()`.
- Add static/dynamic breakpoint placement and deterministic key generation.
- Add explicit cache options through `extra_body`.
- Add cache-validation fallback.
- Add request-shape, key, and fallback tests.

**Deliverable:** Cache-enabled Luna requests use one non-duplicated developer prompt with two ordered static-system cache boundaries.

**Verify:** Assert request kwargs contain one developer message, one block per non-empty section, expected markers/key/options/tools, and no `instructions`; exercise stream and invoke.

### 3. Advancing conversation/tool-result cache boundary

**Approach**

Advance one logical conversation boundary to the latest eligible `input_text` on every request. In normal turns this is the current user input; in a tool loop it is the final typed tool result in the batch. Preserve the previous marker by deterministic input-block fingerprint when adding the new marker, but never exceed four total markers.

Test seam: deterministic Responses input serialization and usage normalization tests; real Bedrock behavior is verified manually after implementation.

**Wiring**

- State: `history_boundary` information flows from `run_loop()`; private previous-marker fingerprint is owned by `BedrockOpenAIClient`.
- Producers: request serialization identifies the latest eligible input block; tool loop supplies batched `function_call_output` items.
- Consumers: Responses API reads the prior marker and writes the new extension; usage extraction reports actual counters.
- Call site: `_stream_once(..., history_boundary=...)` forwards the request boundary; other providers ignore it.

**Edge cases**

- No eligible final input-text block → preserve input shape and omit the conversation marker.
- Tool result is string-only or service rejects typed tool output → omit the marker for that request; do not change tool-loop batching.
- Previous marker no longer matches a historical block → warn, send only the new marker, and rely on reported usage.
- Static + dynamic + old + new markers → allowed maximum of four.
- No subsequent request → report any write cost; do not assume a read.

**Tasks**

- Add optional advancing-boundary plumbing to loop, protocol, providers, and test doubles.
- Serialize tool-result output as typed `input_text` when a marker is eligible.
- Add previous-marker fingerprint retention and four-marker enforcement.
- Add request-shape tests for normal turns, batched tools, marker advancement, and mismatch fallback.
- Add local request-shape and usage-counter regression tests for any issues discovered during manual use.

**Deliverable:** The conversation cache boundary advances across append-only requests without adding model requests or changing transcript/tool-loop behavior.

**Verify:** Inspect marker placement and count in mocked requests; assert normalized cache-read/cache-write usage locally. Verify service behavior manually by using Luna after implementation.

### 4. Billable usage normalization and accounting

**Approach**

Normalize raw provider usage at the adapter boundary into uncached input, cache reads, cache writes, and output. Keep total context input as a derived value for context percentage only. Use the same billable formula in shared and orchestrator accounting.

Test seam: provider usage extraction and public `calculate_cost()`/metrics-row boundaries.

**Wiring**

- State: each provider emits billable usage fields; session tracks billable totals and derived context input.
- Producers: Bedrock Converse, Bedrock Responses, and Ollama adapters.
- Consumers: `run_loop()`, `Session.record_usage()`, TUI usage handling, shared `calculate_cost()`, and `MetricsWriter._build_usage_row()`.
- Call site: `calculate_cost(cost, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)` treats input as uncached input.

**Edge cases**

- Converse raw usage → direct billable mapping.
- Responses raw input total → subtract cache-read/write details before emitting.
- Missing Responses details → cache fields zero.
- Cache portions exceed raw total → zero cache fields and treat raw total as uncached input.
- Zero-cost/local model → total cost remains zero.

**Tasks**

- Update usage semantics and provider mappings.
- Add derived total-context calculation for session context percentage.
- Add shared usage sanitization and billable `calculate_cost()` behavior.
- Update orchestrator metrics cost calculation and tests.
- Update TUI/session/accounting fixtures and add valid/malformed usage tests.

**Deliverable:** All usage consumers calculate identical costs from the four billable token categories without double counting.

**Verify:** Run usage/cost/TUI/orchestrator tests; assert raw Responses total 135/read 30/write 5 emits input 100 and prices normal input for 100.

### 5. Local validation and manual Bedrock verification

**Approach**

Use existing targeted pytest/Ruff workflows for deterministic, offline validation. Verify service behavior manually by using Luna after implementation; do not add network-dependent tests or test-only environment gates.

Test seam: full provider test suite plus local assertions for request shapes, marker placement, usage normalization, and accounting.

**Wiring**

- State: no production or test-only integration state.
- Producers: unit tests produce mocked requests and normalized usage fixtures.
- Consumers: assertions inspect request shapes, events, billable usage, context totals, and cost values.
- Call site: normal local test commands, followed by manual Luna use outside the automated suite.

**Edge cases**

- Manual use discovers unsupported cache placement or unexpected counters → fix the implementation and add a deterministic local regression test.
- Manual verification is unavailable → complete all local validation and record service behavior as unverified rather than adding a network-dependent test.
- Existing unrelated formatting failures → report without modifying unrelated files.

**Tasks**

- Add or update deterministic local tests for request shape, cache markers, usage normalization, and accounting.
- Run targeted tests, full pytest, changed-file Ruff/format checks, and `git diff --check`.
- Use Luna manually with representative user turns and tool loops; inspect the resulting usage/logging and fix any issues found.

**Deliverable:** The implementation has repeatable local validation, with real Bedrock behavior verified through normal manual use rather than network-dependent tests.

**Verify:** Run targeted and full local commands, then manually use Luna to verify cache acceptance, advancing markers, cache read/write counters, and billable usage categories.

## Plan Review

The plan was updated after reviewing prompt stability, append-only cache advancement, typed tool-result placement, the four-marker limit, and billable usage semantics. No blocking implementation decisions remain.

## Status

- Status: planned
- Implementation: not started
- Commit: not requested
