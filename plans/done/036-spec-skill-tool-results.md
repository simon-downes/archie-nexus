# 036 — Skill tool results and reference guidance

## Phase 1 — Objective and Requirements

### Objective

Change skill loading so the skill body is returned in the `skill` tool result and becomes part of the normal conversation history, rather than being inserted into the mutable system prompt. Add a static system-prompt section that explains how to use the skill tool, instructs the model to follow loaded skill guidance, and requires reading relevant reference files instead of making assumptions from the skill body alone.

This keeps the cacheable system prompt stable after session startup while preserving on-demand skill guidance in the model-visible conversation.

### Context

The current implementation discovers a skill catalog at session start, stores loaded bodies in harness state, and rebuilds the dynamic system prompt with those bodies. The `skill` tool returns only a confirmation and a list of reference files. The existing loop already appends tool results to the conversation transcript and sends them through provider history handling.

The current skill guidance also allows a model to see a skill body that points to reference files without making the model read those files. The new static guidance must make the reference-file workflow explicit: reference files are authoritative supplemental material and should be requested through the `references` argument before applying guidance that depends on them.

Relevant code and tests:

- `agent/src/archie_agent/skills.py`
- `agent/src/archie_agent/prompt.py`
- `agent/src/archie_agent/harness.py`
- `agent/src/archie_agent/agents.py`
- `persona/prompts/`
- `shared/src/archie_shared/tool_summaries.py`
- `tests/test_skills.py`
- `tests/test_prompt.py`
- `tests/test_prompt_structured.py`
- `tests/test_prompt_subagent.py`
- `tests/test_tool_summaries.py`
- `tests/test_prompt_caching.py`

### Requirements

- MUST return the extracted skill body in the tool result when `skill` is called with `name` and without `references`
  - AC: The result uses exactly this shape, with frontmatter excluded and the existing `_extract_body()` whitespace normalization preserved:

    ```text
    Skill 'skill-name' loaded. Follow the content inside the `<skill>` tag as guidance for the current task.

    <skill name="skill-name">
    [complete skill body]
    </skill>

    Reference files available:
    - references/example.md
    ```

  - AC: An empty but valid skill body returns the wrapper with an empty body and does not fail
  - AC: The result does not say that the body was loaded into the system prompt

- MUST keep the skill catalog and skill-use guidance in the static system prompt
  - AC: The static prompt includes how to call `skill(name=...)` and `skill(name=..., references=[...])`
  - AC: The static prompt tells the model to follow content inside `<skill>` tags as skill guidance for the current task
  - AC: The static prompt explains that `<reference>` tags contain supplemental reference content, while surrounding plain text is status/information, and tells the model to read relevant references before relying on them
  - AC: The guidance is present whenever the `skill` tool is registered, including a child scope with an empty skill catalog

- MUST keep loaded skill bodies out of system-prompt state
  - AC: Loading a skill does not change `SystemPrompt.static_system` or `SystemPrompt.dynamic_system` for a fixed harness prompt snapshot
  - AC: `prompt_cache_key(system, tool_config)` is unchanged before and after loading a skill when the catalog, static prompt, and tool definitions are unchanged
  - AC: No loaded-skill body is rendered by either root or child prompt builders

- MUST deliver loaded skill bodies through the existing tool-result conversation path
  - AC: The next provider request contains the skill body in ordinary tool-result history
  - AC: The assistant tool call, matching tool result/call ID, and complete wrapped body retain the existing ordering
  - AC: The body is available to later iterations of the same loop and later root user turns through the normal transcript
  - AC: No new event type or parallel skill-history mechanism is introduced

- MUST load skill bodies and requested references through one `skill` call
  - AC: The tool schema accepts a required `name` string and an optional `references` array of relative file-path strings; it does not expose a singular `file` parameter
  - AC: The handler rejects non-list `references` with one plain-text error naming the received value; for a list, it reports every non-string, empty, absolute, or traversal path in input order in one plain-text error result, naming each offending value
  - AC: A call with `references` always loads the skill body first if it is not already loaded, then returns the newly requested references
  - AC: A call without `references` loads only the skill body if it is not already loaded
  - AC: A reference result uses exactly this shape:

    ```text
    <reference name="skill-name" file="references/example.md">
    [complete UTF-8 file content]
    </reference>
    ```

  - AC: References may be requested in the same call that first loads the parent skill body, or in later calls
  - AC: Reference paths are validated relative to the skill directory and are never inferred from the skill body
  - AC: A missing, non-file, unreadable, binary, or path-traversing reference returns an explicit error naming the requested path
  - AC: A batch containing one or more invalid references is atomic: no body or reference key is recorded and the result returns only plain-text errors naming every invalid path, without successful content or an already-loaded manifest
  - AC: This atomic rule applies when a request mixes new references, already-loaded references, and invalid references

- MUST avoid returning duplicate skill bodies or reference contents during a session
  - AC: Loaded content is tracked by `(skill_name, relative_path)`, using the literal reserved relative-path key `__body__` for the skill body
  - AC: A later call returns only newly requested content and a concise manifest of content already loaded
  - AC: A call with no newly requested content returns plain-text status pointing the model to the earlier `<skill>` and `<reference>` tool results
  - AC: No reload parameter or history-compaction behavior is introduced; loaded-content state remains valid for the lifetime of the session
  - AC: Successful reference requests record their reference keys together with the body key when the body was not previously loaded
  - AC: Duplicate requested references within one call are normalized to one request in first-seen order

- MUST preserve existing validation and error behavior
  - AC: Handler validation and file errors remain plain-text error results using the existing tool-dispatch error-string behavior; this change does not alter `ToolResultBlock.is_error` semantics, which remains `False` for these returned error strings in root and child dispatch
  - AC: Unknown skills return an error containing available names
  - AC: Missing names, missing files, path traversal, non-files, and binary files return errors without mutating skill state
  - AC: A valid frontmatter file with no body loads successfully as an empty body
  - AC: A missing, unreadable, or malformed `SKILL.md` returns a plain-text error naming `SKILL.md` and does not record any loaded-content key
  - AC: A valid frontmatter-only `SKILL.md` remains a successful empty-body load and is distinct from malformed or unreadable content

- MUST return an explicit manifest when requested skill content is already loaded
  - AC: The already-loaded response uses plain text outside skill/reference tags, with this shape:

    ```text
    Skill 'skill-name' content is already available in earlier tool results. Use those tagged results from the conversation history.
    Loaded content:
    - skill body
    - references/example.md
    ```

  - AC: The plain-text status identifies the skill body and each loaded reference path in sorted order
  - AC: The plain-text status tells the model to use the earlier tagged tool results in conversation history
  - AC: The plain-text status does not repeat already loaded bodies or reference contents
  - AC: Status lists the body first when loaded, then loaded reference paths in sorted order; newly returned tagged content is emitted before status
  - AC: A mixed request returns the body first when newly loaded, then new references in requested first-seen order, followed by plain-text status listing previously loaded content; duplicate references are omitted from tagged output
  - AC: Skill bodies use the existing extracted-body stripping; reference bodies preserve file bytes decoded as UTF-8 exactly, including empty content and trailing newlines; wrapper boundaries use the literal formats shown above
  - AC: Duplicate detection and content tracking use only session-local state; no prompt-state body storage is introduced

- MUST expose the static skill guidance whenever the skill tool is registered
  - AC: Prompt builders accept an explicit `include_skill_guidance` boolean rather than inferring inclusion from whether the catalog is non-empty
  - AC: Root and child harnesses pass `include_skill_guidance=True` whenever they register the skill tool, including an empty scoped catalog
  - AC: The catalog remains omitted when empty, but the guidance remains present

- MUST apply the same behavior to child agents
  - AC: Child prompts contain the static skill guidance and child-scoped catalog
  - AC: Child skill bodies arrive as child conversation tool results, not dynamic prompt content
  - AC: Child skill results persist for subsequent iterations of the same child loop only; child conversations are not retained for later root user turns

- SHOULD preserve concise UI tool summaries while retaining the full body in the canonical tool-result event
  - AC: `format_tool_complete` identifies the skill or reference and result size without replacing `ToolResultBlock.content`
  - AC: Existing summary escaping and duration behavior remains intact

## Phase 2 — Technical Design

### Overview

Keep the discovered skill catalog and a dedicated skill-use section in the static system prompt. Change the `skill` handler so normal loads return an exact tagged skill body as the tool result. The existing loop, transcript, provider adapters, event persistence, and cache-boundary logic then treat the body like any other tool result.

Reference files remain explicit, opt-in reads. The load result lists them, and the static guidance establishes that a model must request relevant paths through `references=[...]` before relying on instructions that refer to them. Any call that requests references also returns the skill body first when it has not already been loaded.

### Architecture and components

- **Skill discovery and handler — `skills.py`**
  - Owns catalog discovery, skill-body extraction, reference-file validation, exact result formatting, and the `skill` `ToolSpec`.
  - Owns session-local loaded-content keys of `(skill_name, relative_path)`, using the literal `__body__` key for the body; it does not own prompt content.
  - Serializes validation, file reads, deduplication checks, and key commits within one handler lock so concurrent skill calls cannot return or record duplicate content.
  - Returns plain-text status when requested content was already loaded, pointing to earlier tagged tool results in conversation history.
- **Static prompt — `prompt.py` and `persona/prompts/`**
  - Owns the permanent skill-use guidance and constant catalog rendering.
  - Accepts `include_skill_guidance: bool = False` in `build_system_prompt_structured()`, `build_subagent_prompt()`, and the legacy `build_system_prompt()` wrapper. Root and child callers pass `True` exactly when registering the skill tool; the legacy wrapper forwards the value.
  - Places the skill guidance after the general tools section and before the constant `<skills>` catalog; child body/environment/tool sections retain their existing order.
  - Does not render loaded bodies or read loaded-content state.
- **Root and child harnesses — `harness.py` and `agents.py`**
  - Instantiate session-local loaded-content state and pass it to the skill tool.
  - Build prompts from static catalog/guidance only.
- **Conversation loop — `loop.py`**
  - Remains the owner of assistant tool-call and user tool-result ordering.
  - Appends the returned skill body as a normal `ToolResultBlock`.
- **Provider adapters**
  - Preserve the neutral transcript and map the tool result using existing provider-specific formats.

### Data flow

1. Session construction discovers the catalog, registers the skill tool, and builds a static prompt containing skill guidance and the catalog.
2. The model calls `skill(name="...", references=[...])`.
3. The handler validates the name, checks the requested content keys, extracts only new content, records successful keys, and returns exact `<skill>`/`<reference>` results plus a manifest for already-loaded content.
4. The loop appends the result as a `ToolResultBlock` after the matching assistant tool call.
5. The next request receives the body and references through conversation history; the system prompt remains unchanged.
6. If the model requests only content already loaded, the handler returns a concise manifest pointing to the earlier tagged tool results rather than duplicating them.
7. The harness persists the result through the existing canonical tool-result event path.

### Cache and provider contract

- `prompt_cache_key(system, tool_config)` hashes the static system prompt and tool definitions only; it must not change after a skill load.
- The developer/system message must remain identical after a skill load for a fixed harness snapshot.
- The request input gains a normal tool-result item containing the skill body.
- Conversation cache markers may advance or move according to the existing history-boundary algorithm; this is separate from prompt-cache-key stability.
- No dynamic developer block or skill-body developer cache breakpoint is emitted.
- Provider-neutral acceptance is `ToolResultBlock` content in the transcript. Provider-specific tests assert:
  - Responses: `function_call_output.output[].text` contains the wrapper and body.
  - Converse: the provider tool-result content contains the wrapper and body.
  - Ollama: the `role: tool` message contains the wrapper and body.

### Error handling and edge cases

- Valid frontmatter with an empty body → successful empty `<skill>` result.
- Missing, unreadable, or malformed skill file at load time → error result; loaded-content key set is unchanged.
- Malformed or unreadable extraction is distinguished from a valid frontmatter-only empty body by making body extraction return a typed failure/result rather than silently returning an empty string.
- Unknown skill → error with sorted available names; no state mutation.
- Reference request before parent body load → loads the body first in the same call; if any requested reference is invalid, the atomic call returns errors and records no body or reference keys.
- Reference path traversal, missing path, non-file, or binary file → existing error result; no state mutation.
- Skill result containing instruction-like text → remains untrusted guidance; authorization and side-effect policy remain enforced by code.
- Concurrent tool calls for one skill → the closure lock serializes state changes; `run_loop()` retains result positions and call IDs when appending ordered results.
- Interrupted or failed turn → existing tool-result repair and persistence behavior applies; no prompt mutation occurs.

### Code structure and compatibility

- Extend `agent/src/archie_agent/skills.py`; do not create a separate skill runtime module.
- Add one concise static prompt fragment under `persona/prompts/` and include it from `prompt.py` whenever the skill tool is registered.
- Remove `loaded_skills` body parameters and `_build_loaded_skills()` from prompt builders, their imports, and all tests/callers. There are no external API compatibility requirements in this repository; obsolete body-bearing prompt APIs should be removed rather than silently ignored. Update `tests/test_prompt.py`, `tests/test_prompt_structured.py`, and `tests/test_prompt_subagent.py` accordingly.
- Replace `list[tuple[str, str]]` wiring with `set[tuple[str, str]]` loaded-content keys in root and child construction; use a reserved relative-path key for the main body. Add `include_skill_guidance=True` to prompt-builder calls whenever the skill tool is registered.
- Reuse existing `ToolSpec`, `ToolResultBlock`, event-factory, tool-summary, and path-validation patterns.

### Key decisions

- **Tool-result history over dynamic developer prompt:** keeps the system prompt and prompt-cache key stable while using the existing transcript and provider history path.
- **Loaded-content keys over stored bodies:** prevents skill content from becoming mutable prompt state and avoids repeated body insertion while allowing body and references to be deduplicated independently.
- **Batched explicit references over automatic concatenation:** lets the model request known supplemental material in one call while keeping reference selection explicit and making the obligation to inspect authoritative references visible.
- **Module-level static guidance over per-skill body instructions:** ensures the model knows how to use the reference workflow before loading any skill.

## Phase 3 — Milestones

### Milestone 1 — Add static skill-use and reference-reading guidance

**Approach**

- Add one concise prompt fragment under `persona/prompts/` and include it from `prompt.py` whenever the `skill` tool is registered.
- The section must describe both `name` and `references`, explain the distinction between `<skill>`/`<reference>` content and surrounding plain-text status, state that `<skill>` content is task guidance to follow, and state that relevant references must be read before relying on them.
- Keep the discovered `<skills>` catalog separate and constant; include guidance even when a registered child skill tool has an empty scoped catalog.
- Test seam: `build_system_prompt_structured()` and `static_system.text`.
- ⚠️ Do not include any loaded skill body or mutable loaded marker.

**Wiring**

- State: `include_skill_guidance` is a prompt-builder input, not mutable session content.
- Producers: root and child harness construction sets it to `True` when registering the skill tool.
- Consumers: prompt assembly includes the static guidance; the catalog is appended only when non-empty.

**Edge Cases**

- Empty root or child catalog with registered skill tool → guidance remains present and the catalog section is omitted.
- Skill tool not registered → guidance is omitted.

**Tasks**

- Add the static skill guidance prompt fragment.
- Include it in root and subagent static prompt assembly.
- Replace tests that assert loaded bodies in prompt output with static-guidance and no-body assertions.

**Deliverable:** Static root and child prompts contain the skill-use and reference-reading instructions.

**Verify:** Run `uv run pytest tests/test_prompt.py tests/test_prompt_structured.py tests/test_prompt_subagent.py -q`; inspect `static_system.text` and confirm it contains the guidance while loaded body text is absent.

### Milestone 2 — Return exact skill and reference content from the skill tool

**Approach**

- Update `create_skill_tool()` in `skills.py` to accept `references: list[str] | None`; remove the singular `file` schema/property and handler path.
- Track loaded-content keys and return the exact `<skill>` and `<reference>` formats defined in Requirements.
- Validate and read all requested new references before committing any keys or returning successful batch content.
- Treat reference requests as body-dependent: if the body is not loaded, load it in the same call before returning references; record successful body and reference keys together.
- Serialize the complete read/validate/format/commit operation with an `asyncio.Lock` captured by the `create_skill_tool()` closure; preserve the existing result-string error behavior.
- Test seam: the public `ToolSpec.handler` result and captured loaded-content key state, including concurrent calls.

**Wiring**

- State: session-local `set[tuple[str, str]]` of loaded `(skill_name, relative_path)` keys, created by each root harness and child dispatch.
- Producers: a successful atomic batch adds the body key if needed and each newly returned reference key; invalid batches and already-loaded requests do not mutate the set.
- Synchronization: each `create_skill_tool()` closure owns one `asyncio.Lock`; the lock covers validation, reads, result construction, and commit, so the returned content and recorded keys are atomic. Root and each child invocation own separate lock/set pairs.
- Consumers: duplicate-load checking and plain-text status generation read the set; prompt builders do not read it.
- Call sites: update root and child `create_skill_tool(catalog, loaded_content_keys)` construction and pass `include_skill_guidance=True` to prompt builders.

**Edge Cases**

- Valid empty body → successful empty wrapper.
- Missing/unreadable/malformed body → error and no state mutation.
- Duplicate-only request → exact plain-text status, with no repeated body or reference content.
- Reference request before body load → body is loaded first in the same call; an invalid reference causes the whole call to fail atomically.
- Duplicate references in one request → one first-seen request.
- Any invalid reference in a batch → plain-text errors naming all invalid paths and no state mutation or successful content returned.
- Concurrent calls requesting the same content → serialized handling; only the first successful call returns and records the content.

**Tasks**

- Change handler state and signatures from body-bearing lists to loaded-content key sets.
- Replace the singular `file` argument with `references: list[str] | None`.
- Implement exact body-first load, reference, and plain-text status output with deterministic ordering and atomic batch validation.
- Update the skill tool description to describe tool-result delivery and reference reading.
- Add handler tests for exact outputs, extraction failures, duplicate behavior, reference ordering, duplicate normalization, malformed `references` inputs, atomic batch failure including mixed invalid/already-loaded requests, unchanged validation errors, and concurrent duplicate requests.

**Deliverable:** The skill tool returns deterministic, clearly marked skill or reference content without storing bodies in prompt state.

**Verify:** Run `uv run pytest tests/test_skills.py -q`; assert exact result strings, schema shape, atomic state transitions, and loaded-content keys.

### Milestone 3 — Route loaded content through root and child conversation history

**Approach**

- Remove loaded-body rendering and body parameters from `build_system_prompt_structured()` and `build_subagent_prompt()`.
- Update `harness.py` and `agents.py` to use loaded-content key state while preserving catalog scoping and pass explicit skill-guidance inclusion to prompt builders.
- Use the existing `run_loop()` tool-result append path and event persistence; do not add skill-specific loop or event handling.
- Test seam: harness/loop boundary, capturing the provider-neutral `Turn` list and `SystemPrompt` passed to the provider.
- ⚠️ Child coverage is limited to subsequent iterations of the same child loop; child conversations are not retained across later root turns.

**Wiring**

- State: the session transcript owns returned skill content as `ToolResultBlock` content; prompt state owns only static catalog/guidance.
- Producers: `run_loop()` appends the result after the assistant call; the harness persists the canonical tool-result event.
- Consumers: the next provider request reads the transcript; prompt construction ignores loaded names and bodies.
- Call sites: root `_build_prompt()` and child `build_subagent_prompt()` no longer receive loaded bodies.

**Edge Cases**

- Skill load during a multi-iteration turn → body appears in the next iteration’s tool-result history; system prompt is unchanged.
- Skill load followed by a later root user turn → body remains in the normal root transcript.
- Child skill load → body is visible in later iterations of that child loop only; each child owns and discards its own key set and lock when the child invocation ends or is interrupted.

**Tasks**

- Remove `_build_loaded_skills()` and loaded-body prompt parameters and update all callers/tests.
- Update root and child harness wiring to use loaded-content key state.
- Add a root integration test asserting the assistant call/result pairing and provider-neutral tool-result content.
- Add child integration coverage for the second child request.

**Deliverable:** Root and child models receive loaded skill content exclusively as ordinary tool-result history.

**Verify:** Run focused harness/loop tests and assert the captured second request contains the matching `ToolResultBlock` wrapper/body while both prompt sections contain no body.

### Milestone 4 — Preserve provider cache and canonical event behavior

**Approach**

- Reuse the existing prompt-cache key and advancing conversation-boundary tests at the provider request seam.
- Assert prompt-key/developer-message stability separately from conversation-marker movement.
- Assert provider-specific wire shapes for Responses, Converse, and Ollama while retaining one neutral transcript contract.
- Confirm canonical tool-result events persist the exact returned content and replay through the existing history path.
- Test seam: mocked provider request payloads and canonical session-log readback.
- ⚠️ Do not use a live provider; verify deterministic payloads and existing usage/accounting seams offline.

**Edge Cases**

- Empty dynamic system → no dynamic developer block or dynamic breakpoint.
- Skill body containing tool-like text → remains tool-result content and does not alter tool definitions or authorization.
- Replayed skill result → content is available as history without rebuilding the system prompt.

**Tasks**

- Add prompt-cache request-shape tests for a loaded skill result.
- Add Responses, Converse, and Ollama history-shape assertions.
- Add persistence/replay coverage for the canonical tool-result content.
- Update `format_tool_complete` tests for successful body load, body-plus-reference batch, duplicate/status response, atomic reference error, and summary size without changing stored content. The summary should identify the skill and aggregate result size rather than enumerate full reference bodies.
- Update architecture documentation to describe tool-result loading, batched references, and the body-first reference contract.
- Run complete tests and lint.

**Deliverable:** Skill tool results preserve the static prompt/cache contract and canonical history across all supported providers.

**Verify:** Run `uv run pytest` and `uv run ruff check .`; inspect mocked requests and session-log readback for unchanged developer prompt/cache key and complete wrapped tool-result content.

## Phase 4 — Review

Review this plan against the repository’s current skill, prompt, loop, child-agent, event, and provider-cache contracts before implementation. The review must verify exact result formats, reference-file ordering and state behavior, provider-neutral versus provider-specific assertions, and removal of obsolete dynamic-prompt APIs.

Approval gate: approve the objective and requirements, then the technical design, then the milestone plan before implementation.