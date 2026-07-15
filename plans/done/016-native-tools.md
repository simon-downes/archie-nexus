# Plan 016 — Native First-Class Tools (Hybrid: native + code-mode)

## Motivation

Session analysis of `archie-01kxe7k3ne` (memory: `session-analysis-01kxe7k3ne.md`)
showed the harness funnels **everything** through `exec`: 58% of execs wrapped a
single `read`, 67% were same-file re-reads, only 3/116 used `asyncio.gather`. This
is the worst-case configuration — it pays code-mode overhead (subprocess spin-up,
codegen, boilerplate `async def main()`) **without** the code-mode benefits
(data-locality filtering, chaining, loops).

Industry consensus (memory: `code-mode-research.md`; Anthropic "Code execution with
MCP", Cloudflare "Code Mode") is a **hybrid**: keep a small set of frequent, simple
primitives as native first-class tools for the single-op case; reserve `exec`/code
mode for genuine multi-step / filter / loop work. Real coding agents (Claude Code
etc.) expose Read/Edit/Grep/Bash natively **and** have a code path.

## Goal

Expose the existing `@tool`-decorated async primitives (`read`, `grep`, `glob`,
`edit`, `write`, `shell`, `web_fetch`, `web_search`, code-intelligence tools) as
**native first-class tools** the LLM can call directly, **while keeping** them
available inside `exec`.

### Hard constraints (from user)
1. **Single source of truth** — native and code-mode paths resolve to the *same*
   underlying function. No duplicated implementations, no divergence.
2. **Identical input signatures** — the arguments are used the same way in both
   paths. The native JSON schema is *generated from* the function signature, not
   hand-written.

### Reference patterns
- **maki** (`archie-nextgen/_research/maki`) — strong match: a `#[derive(Tool)]`
  single-source pattern where native + interpreter paths share one dispatch, schema
  auto-generated from the signature. We replicate the *idea* in Python via
  `inspect.signature` + type-hint→JSON-schema.
- **tau** (`archie-nextgen/_research/tau`) — simpler, hand-written schemas,
  native-only. Explicitly *not* what we want (violates single-source).

## Current architecture (what we build on)

Already in place — this refactor is **additive**, mostly a schema generator +
registration change:

- `exec/tools/__init__.py` — `@tool(guidelines=(...))` decorator registers async
  fns into `_TOOLS`; `get_all_tools()` → name→fn; `get_tool_guidelines()`. Typed
  exception hierarchy (`ToolError` → `PathValidationError`, `FileNotFoundError`,
  `BinaryFileError`, `EditError`, `ContentTypeError`, `UnsupportedLanguageError`,
  `FileTooLargeError`).
- `exec/tools/{fs,shell,web,code}.py` — all primitives are `@tool` async fns with
  typed signatures + docstrings. Paths are `/workspace`-relative. They **raise** on
  error (never return error strings). Pure functions — safe to call in-process.
- `tools.py` — provider-neutral `ToolSpec(name, description, schema, handler)` +
  `ToolRegistry`; `to_tool_config()` → `[{name, description, input_schema}]`.
- `exec/tool.py` — `create_registry()` registers ONLY `exec` today.
  `_generate_tool_docs()` (`:144`) **already** introspects `inspect.signature` +
  `inspect.getdoc` to build the exec doc listing → the schema generator reuses this
  exact introspection.
- `harness.py` — `create_registry()` (`:115`), registers `skill` (nearby in
  `__init__`), `to_tool_config()` (`:118`). Dispatch in `_execute_tool` (`:323`):
  `spec = self._registry.get(block.name)` (`:329`); exec special-cased (`:346-347`,
  uses `run_exec` + `format_result` + `on_start`); generic path
  `result = await spec.handler(**block.input)` (`:351`), `content = str(result)`
  (`:352`), exception → `content = f"{type(e).__name__}: {e}"`, `is_error=True`
  (`:353-356`). (Line refs current as of this session; harness was refactored after
  plan 015 — treat as approximate, grep the symbol names if they drift.)
- `runner.py` — `_build_namespace()` (`:64`) injects `get_all_tools()` fns
  (audit-wrapped by `_wrap_for_audit` `:94`) into model-code namespace. **Unchanged.**
- `prompt.py` — uses `get_tool_guidelines()`.

### Key topology note
Exec tool fns were written to run **inside the runner subprocess**. For native
dispatch they run **in-process in the harness** (host/container). This is safe:
they're pure async fns with no subprocess-only assumptions. `/workspace` exists in
both (nexus *is* the container — VISION §72). Native path skips the audit wrapper
(that's runner-only for code-mode auditing) — acceptable; the harness already
records tool_use/tool_result via session persistence.

## Design

### Single source of truth
One function per capability in `exec/tools/*.py`. Two consumers:
- **code mode**: `runner._build_namespace()` injects them (existing).
- **native**: a new `native.py` generates a `ToolSpec` per fn whose `handler` is the
  fn itself, and whose `schema` is generated from `inspect.signature`.

Both call the identical fn object → single source, identical signatures guaranteed
by construction.

### Which tools go native vs code-mode-only?
Per research, native = frequent, simple, single-op. Decision:

| Tool          | Native | Rationale |
|---------------|--------|-----------|
| `read`        | ✅ | most common single op (58% of execs) |
| `grep`        | ✅ | frequent single op |
| `glob`        | ✅ | frequent single op |
| `edit`        | ✅ | frequent single op |
| `write`       | ✅ | frequent single op |
| `shell`       | ✅ | frequent single op |
| `web_fetch`   | ✅ | single op; but see filtering note* |
| `web_search`  | ✅ | single op |
| `code`        | ✅ | single tool `code()` (not a family — verified `code.py:374`) |

`exec` stays native (unchanged). *`web_fetch` returns large payloads best filtered
in-sandbox — guidance should still steer bulk fetches toward `exec`. Native is a
convenience for the single-shot case.

**Verified tool count**: fs (read/grep/glob/write/edit) + shell + web_fetch +
web_search + code = **9** native tools + `exec` + `skill` = 11 total. Well under the
~50-tool accuracy cliff — no progressive disclosure needed for now.

**Opt-in marker**: add a `native: bool = True` param to `@tool(...)`, so a tool can
be made code-mode-only later (future-proofing if a large family is added). All 9
current primitives use the `True` default. NOTE: since no production tool sets
`native=False` today, the flag is exercised only by a **test-only dummy tool**
marked `native=False` (see M1 tests) — it is otherwise dead code and that is
acceptable as deliberate future-proofing.

### Return model — STRING-ONLY, formatted in the tool fn (MAJOR DECISION)
**Every native tool fn returns a formatted `str`.** This replaces the earlier
"return `list`/`dict`, `json.dumps` in the handler" design. Rationale (verified
against 3 reference agents): **maki** (`_research/maki`) plugins return a string and
code mode calls the *same* handler — "tools return strings, parse output yourself";
**archie-nextgen `main`** (`src/archie/tools/*.py`) — THE model — has all 9 as native
tools each returning a formatted string. Structured returns (`list`/`dict`) were
verified (subagent) to have **no non-test consumers**: TUI shows byte-count + string
summary only, audit records call not value, `format_result`/runner serialization is
shape-agnostic, and no tool consumes another's structured return. Only tests and
model-written code-mode scripts break — and the *string format becomes the contract*.

Benefit: the LLM sees the **identical** rendered output whether it calls a tool
natively or via `exec`; `_native_handler` collapses to near-trivial (str passthrough
+ truncate). Single source of truth is *stronger*: one fn → one string → both paths.

**Return-type changes (verified fs.py/shell.py/web.py/code.py):**
- `read` → `str` (fs.py:72) — **KEEP** current format.
- `edit` → `str` (fs.py:300) — **KEEP** unified diff (git-style, 3 ctx lines, bounded
  by edit size; fs.py:354-365 `difflib.unified_diff`). Model self-verifies the change.
- `web_fetch` → `str` (web.py:17) — **KEEP** text but PREPEND header
  `URL: {url}\nType: {ct} · {size}\n\n{text}`.
- `grep` → **CHANGE `list[dict]`→str** (fs.py:146). Grouped by file: `{relpath}:`
  then `  {lineno:>w}| text` (match) / `:` (context); blank line between files;
  `No matches found.`; **mtime-desc** sort; cap ~50 groups + narrow marker.
- `glob` → **CHANGE `list[str]`→str** (fs.py:235). Header `{N} files, most recent
  first` + `{relpath}` per line; **mtime-desc** sort (was alpha); `No files found.`;
  cap ~100 + narrow marker.
- `web_search` → **CHANGE `list[dict]`→str** (web.py:91). Blocks joined `\n\n`, each
  `{i}. {title}\n   {url}\n   {snippet}`; 1-indexed; `No results found.`
- `code` → **CHANGE `list[dict]`→str** (code.py:374). Outline
  `{path} ({lang}, {N} lines)` + `{indent}{signature} [line a-b]`; search
  `{rel}:{a}-{b} — {sig}`; cap ~200 symbols + narrow marker.
- `shell` → **CHANGE `dict`→str** (shell.py:28). `$ {command}\n[exit: {code}]\n{output}`
  (stdout+stderr combined).
- `write` → **CHANGE `None`→str** (fs.py:280). `Written: {path} ({N} lines)` (N = line
  count). Backwards-compatible for exec code that ignored the return value.

**Truncation strategy**: cap WHOLE UNITS *before* formatting (grep ~50 groups, glob
~100 files, code ~200 symbols, read 500-char lines) each with a
"showing X of Y — narrow your query" marker (better than blind tail-chop). Keep the
existing `_MAX_RESULT_CHARS=16_000` global backstop in `_native_handler`
(mirrors `format_result`, tool.py:139). No artifact-spill / `retrieve_artifact`
(deferred, separate feature).

`web_fetch` raises builtin `ConnectionError`/`TimeoutError` (web.py:33-34, not
`ToolError` subclasses); `read` raises the tools-module `FileNotFoundError`
(`__init__.py:26`, shadows the builtin). Harness catches all `Exception` so both
format fine as `{type}: {msg}`.

### Signature → JSON schema (the new code)
`native.py::_schema_from_signature(fn) -> dict`:
- Iterate `inspect.signature(fn).parameters`.
- Map Python type hints → JSON schema types:
  - `str`→`string`, `int`→`integer`, `bool`→`boolean`, `float`→`number`,
    `list`/`list[...]`→`array`, `dict`→`object`.
  - `X | None` / `Optional[X]` → underlying type, **not** required.
  - No annotation → `string` (log a warning; all current params are annotated).
- `required` = params with no default AND not Optional.
- Description per property: parse the docstring `Args:` section (Google style) if
  present; else omit. (Reuse a small docstring-args parser; keep it lenient.)
- Tool `description` = `inspect.getdoc(fn)` first paragraph (same as
  `_generate_tool_docs` first-line, but allow full first paragraph for native).

`native.py::make_native_specs() -> list[ToolSpec]`:
- `for name, fn in get_all_tools().items(): if getattr(fn, "_native", True): yield
  ToolSpec(name, description=..., schema=_schema_from_signature(fn), handler=fn)`.

### Registration
`exec/tool.py::create_registry()` registers `exec` **plus** every native spec.
Harness `create_registry()` call (`:115`) unchanged; the extra specs just appear in
`to_tool_config()`.

### Dispatch — handler wrapping (near-trivial now)
The generic branch (`:351`) does `result = await spec.handler(**block.input)` then
`content = str(result)` (`:352`). Because every native fn now **already returns a
formatted `str`**, `_native_handler` no longer does type-based stringification — it
just awaits the fn, applies the global truncation backstop, and returns the string.

**Decision**: `ToolSpec.handler` for native tools points to `_native_handler(fn)`
(the wrapper), NOT the raw `@tool` fn — so the global `_MAX_RESULT_CHARS` backstop is
applied on the native path. The harness's `str(result)` at `:352` is a **no-op**
(`str(already_a_string) == already_a_string`), so no harness change is needed and
single-source is preserved (the wrapper delegates to the same fn code mode uses).

- Result-size cap: import `_MAX_RESULT_CHARS = 16_000` from `exec/tool.py`; the
  wrapper truncates to it with the `format_result` marker (tool.py:139). Per-tool
  unit caps (above) are applied *inside* each fn, before this backstop.

### Prompt / guidelines
`prompt.py` already injects `get_tool_guidelines()`. Add guidance:
- "Prefer native tools (`read`/`grep`/`glob`/`edit`/`write`/`shell`) for single
  operations. Use `exec` when you need to chain steps, filter large output before
  returning, or loop — and batch independent calls with `asyncio.gather`."
This directly counters the observed anti-pattern.

## Milestones

### M1 — Schema generator + native spec factory + string-returning tool fns
- New `agent/src/archie_agent/exec/native.py`:
  - `_schema_from_signature(fn)` — type-hint→JSON-schema + docstring Args parsing.
  - `_docstring_arg_descriptions(fn)` — lenient Google-style `Args:` parser.
  - `_native_handler(fn)` — async wrapper: `await fn(**kw)`, then apply the global
    `_MAX_RESULT_CHARS` truncation backstop (import from `exec/tool.py`; same marker
    as `format_result`, tool.py:139), **return a `str`**. No type-based
    stringification — fns already return strings. `None` → `""` kept only as a
    defensive fallback.
  - `make_native_specs() -> list[ToolSpec]` — `handler=_native_handler(fn)` wrapper,
    so the harness `str(result)` at `:352` is a no-op.
- Extend `@tool` decorator (`exec/tools/__init__.py`) with `native: bool = True`;
  store `fn._native`. `get_all_tools` unaffected.
- **Convert tool fns to return formatted strings** (per "Return model" section):
  - `grep` (fs.py:146) `list[dict]`→str, grouped-by-file, mtime-desc, ~50-group cap.
  - `glob` (fs.py:235) `list[str]`→str, mtime-desc, header + ~100 cap.
  - `write` (fs.py:280) `None`→`Written: {path} ({N} lines)`.
  - `shell` (shell.py:28) `dict`→`$ {cmd}\n[exit: {code}]\n{output}`.
  - `web_search` (web.py:91) `list[dict]`→numbered blocks.
  - `web_fetch` (web.py:17) prepend `URL/Type/size` header to existing text.
  - `code` (code.py:374) `list[dict]`→outline/search string, ~200-symbol cap.
  - `read` (fs.py:72) and `edit` (fs.py:300) — **no change** (already good strings).
- All 9 primitives stay native (count verified — no cliff risk).
- Tests:
  - **NEW unit test per format fn** (the string format is now the contract):
    grep grouped/empty/cap, glob header/mtime-order/cap, write marker, shell exit
    line, web_search numbering/empty, web_fetch header, code outline/search/cap.
  - schema shape for each param kind (required/optional/typed/Optional/list);
    docstring-desc extraction; global truncation with marker; `native=False`
    exclusion **using a throwaway test-only tool** marked `native=False`.

### M2 — Register native specs
- `exec/tool.py::create_registry()` registers `exec` + `make_native_specs()`.
- Guard name collisions (e.g. no native tool named `exec`/`skill`).
- Tests: `create_registry().to_tool_config()` includes native tools with correct
  schema; `exec` + `skill` still present; count matches expectation.

### M3 — Dispatch integration + rewrite shape-asserting tests
M3 is **integration testing through the harness** — `_native_handler` (M1) is trivial
(str passthrough + backstop). M3 confirms end-to-end via `_execute_tool`.
- Ensure generic dispatch (`harness.py:351-356`) formats native errors identically
  to exec (`{type}: {msg}`, `is_error=True`) — add test hitting the tools-module
  `PathValidationError` via a native `read` on an out-of-workspace path (assert the
  content shows `PathValidationError:`, from the tools module not the builtin).
- Confirm `str(result)` at `:352` is a no-op (fn/wrapper already returned a string).
- Tests through `_execute_tool`: native `read`, `grep`, `glob`, `code`, `shell`,
  `edit`, `write`, `web_search`, `web_fetch` — assert **string content** (not shape),
  happy path + error path, including global truncation of a large result.
- **REWRITE existing shape-asserting tests** to assert string content:
  `test_exec_tools.py:198,212` (glob), `:228-238` (grep), `:263-300` (shell);
  `test_web_tools.py:411-433` (web_search); `test_code_tool.py:457-517` (code shape).

### M4 — Prompt / guidelines update
- Add native-vs-exec guidance to `get_tool_guidelines()` (or `prompt.py`).
- Ensure `_build_exec_description()` still lists all code-mode fns (including native
  ones — they remain available in exec).
- Test: system prompt contains the native-preference guidance; exec description
  unchanged/complete.

### M5 — Integration + verification
- Full test suite (repo-root `tests/`), `ruff`.
- `FakeLLMClient` scenario: model emits a native `read` ToolUse → harness dispatches
  → correct ToolResult; then an `exec` that also calls `read` → both work (proves
  single source, both paths).
- Manual Bedrock smoke: confirm native tools appear in `toolConfig` and the model
  can call `read` directly and via `exec`.
- **TUI summary (confirmed gap)**: `harness._summarise_tool_input` (`:479`) handles
  only `exec`/`skill` and falls through to `""` for everything else → native tool
  calls render blank in the TUI live broadcast. Add a generic branch: for native
  tools summarize a salient arg by a fixed key-priority order for determinism:
  `path` → `command` → `pattern` → `url` → `query` → first input value. Add a
  test asserting a non-empty summary for a native `read`.
  NOTE: `_summarise_tool_input` is called at `:220` and feeds `ToolCallEvent`
  (**live broadcast only**). History is persisted separately by `_persist_tool_call`
  (`:218`) which JSON-serialises the raw name+input — there is no `_tool_summaries`
  dict (removed in a post-015 refactor). So this fix affects the live TUI stream
  only; `/history` needs no change here.

## Non-goals / deferred
- Progressive tool disclosure / `search_tools` (only needed if the catalog grows
  past the ~50-tool accuracy cliff; `native=False` keeps us well under it for now).
- Reworking the audit wrapper for native calls (code-mode-only concern).
- Provider shaping changes (registry stays neutral; bedrock.py already shapes).

## Risks
- **Type-hint→schema gaps**: unusual annotations (unions of 3+, `Literal`, custom
  types) unsupported. Mitigation: current primitives use only str/int/bool/Optional
  — cover those; `raise`/warn on unknown so it's caught in tests, not at runtime.
- **Tool-count accuracy cliff**: not a risk now (11 total tools, verified); `native`
  flag mitigates future large additions.
- **Result-size / context bloat** from native returns: mitigated by shared
  truncation (`_MAX_RESULT_CHARS`).
- **TUI live summary** blank for native tools (`_summarise_tool_input` fall-through)
  — fixed in M5. `/history` is unaffected (separate `_persist_tool_call` path).
- **Docstring parser fragility** (most likely M1 bug source): must tolerate
  multi-line param descriptions, indented continuation lines, and `param (type):`
  annotations in the `Args:` block. Keep it lenient — on any parse ambiguity, omit
  the description rather than raising. Cover these cases in M1 tests.

## Files touched
- NEW `agent/src/archie_agent/exec/native.py`
- `agent/src/archie_agent/exec/tools/__init__.py` (`@tool` gains `native`)
- `agent/src/archie_agent/exec/tool.py` (`create_registry` registers native specs)
- `agent/src/archie_agent/prompt.py` or guidelines (native-preference guidance)
- `agent/src/archie_agent/exec/tools/fs.py` (`grep`/`glob`→str, `write`→str; `read`/`edit` unchanged)
- `agent/src/archie_agent/exec/tools/shell.py` (`shell`→str)
- `agent/src/archie_agent/exec/tools/web.py` (`web_search`→str, `web_fetch` header)
- `agent/src/archie_agent/exec/tools/code.py` (`code`→str)
- `agent/src/archie_agent/harness.py` (`_summarise_tool_input` generic branch)
- `tests/…` (new format-fn unit tests + integration tests; rewrite shape-asserting tests)
