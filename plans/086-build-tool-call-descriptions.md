# 086 — Descriptions on shell and exec calls

Status: planning
Branch: build-it/086-tool-call-descriptions (base: feat/default-tui-entrypoint @ 68ed85c)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/086-tool-call-descriptions

## Objective

The TUI's shell rows show the raw command and exec rows show `python · N lines`, so the user
has to read commands or open the detail view to see what the model is doing. This build adds
a model-written `description` input to the `shell` and `exec` tools and shows it in the tool
summaries instead of the command or `python`, falling back to today's text when a call has
no description.

## Context

The user's words: "add description/purpose inputs to the shell and exec tools so that the
model can articulate the reason for running the commands / what they do. The main aim is
for me to easily see what the model is doing. Descriptions should then be shown in the tui
tool summary rather than the existing commands or 'python'."

Codebase facts:

- Tool rows: `shared/src/archie_shared/tool_rows.py` (`_shell`, `_exec` set line 2),
  drawn by `cli/src/archie_cli/tui/transcript_view.py` `ToolRow`. Rules and examples are in
  `plans/tui-tool-rows.md`, which says "shell and exec rows have no `description` input";
  `tests/test_tool_rows.py` checks every example there.
- Other summaries: `shared/src/archie_shared/tool_summaries.py` (`format_tool_pending`,
  `format_tool_activity`, `format_tool_complete`) and `shared/src/archie_shared/transcript.py`
  (`tool_headline`, `tool_crumb`, subagent activity). These feed subagent status lines and
  detail headers in the TUI, and the orchestrator's web session viewer.
- Schemas: `shell`'s native schema is generated from the `@tool` function signature and
  docstring (`agent/src/archie_agent/exec/native.py`); the same function is also callable
  inside exec scripts. `exec`'s schema is a literal in `agent/src/archie_agent/exec/tool.py`
  `create_registry`. The Kiro backend serves the same registry over MCP
  (`agent/src/archie_agent/kiro/mcp_server.py`), and its translator passes `rawInput`
  through as the canonical `tool_call` input.
- Dispatch already tolerates an extra key: `_native_handler` drops kwargs the function
  doesn't declare, and every exec dispatch path (`harness.py`, `agents.py`, `mcp_server.py`)
  reads only `source`.
- Prior art in `_research/`: opencode had a required `description` on bash ("Clear, concise
  description of what this command does in 5-10 words", with examples) and later removed it;
  tau requires a present-participle one ("Running tests"); maki has an optional "What the
  command does in 3-5 words, not the command itself" and treats a description equal to the
  command as missing; Amazon Q had an optional `summary`. None puts one on a code-exec tool.
  UIs show the description in the header and keep the command available, with "in <dir>"
  appended and a fallback to the command or a generic label.
- Brain: `projects/archie/nexus-roadmap/tui-prototype.md` notes that a `description`
  parameter would need an agent change.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie/` and `.ruff_cache/` are gitignored)
- Failing before this work: 23 tests, all timing/concurrency failures in this container:
  10 in `tests/test_question_tool.py`, 13 (4 failed, 9 errors) in `tests/test_ws_integration.py`.
  List saved at `/tmp/086-baseline-fail.txt`. `ruff check` passes.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout, unrelated).

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Required or optional in the schemas | Required on both `shell` and `exec` | Optional | The aim is that every call is readable at a glance; optional fields get skipped. On Kiro the MCP server rejects a call without it and the model retries; retries can be counted from session logs | user Q1 |
| D2 | Which summaries use it | Every summary built by the shared formatters: TUI tool rows, subagent status lines, detail header and breadcrumb, web viewer rows | TUI tool rows only | All of them derive from `tool_summaries.py`/`transcript.py`; changing one would leave a subagent line saying `Shell git status` under a row saying "Check for uncommitted changes" | user Q2 |
| D3 | Input name | `description` | `purpose`, `summary`, `justification` | `plans/tui-tool-rows.md` and the brain note already use `description`, as do opencode, tau and maki | self |
| D4 | Where the input is declared | Added to the model-facing `ToolSpec` schemas only (native `shell` in `native.py`, `exec` in `tool.py`); the `shell()` function signature is unchanged | A `description` parameter on `shell()` | The same `shell()` is called from exec scripts, where a required description is noise and would break existing scripts. `_native_handler` already drops undeclared kwargs and every exec dispatch reads only `source`, so no dispatch change is needed | self |
| D5 | Property order | `description` listed first in `properties` | After `command`/`source` | The model states the purpose before writing the command or script, and the short field leads the JSON in logs | self |
| D6 | Display text | Whitespace collapsed to one line, `/workspace` stripped on rows like every other target; blank or non-string counts as missing | Truncate to N words; treat a description equal to the command as missing (maki) | Rows already cut to width; the schema text tells the model not to repeat the command, and a repeat would still be readable | self |
| D7 | Shell row line 2 with a description | The description, then the existing ` · in <dir>` suffix | Description plus `$ command` | The user asked for the description "rather than" the command; the command stays in the detail view and in copy | self |
| D8 | Exec row line 2 with a description | The description only | Description plus ` · python · N lines` | Same reason as D7; the source is in the detail view | self |
| D9 | Model guidance | The schema property's description only | Also a system-prompt guideline | The property text is shown with every tool definition; a guideline would add prompt tokens for the same instruction | self |

## Requirements

- MUST list `description` as a required string property of the `shell` and `exec` tool
  schemas sent to the model, for every backend that uses `create_registry()` (Anthropic,
  OpenAI, Ollama, and Kiro over MCP).
  - AC: `create_registry().get("shell").schema` and `.get("exec").schema` have
    `description` in `properties` (type `string`, with the guidance text) and in `required`.
  - AC: the Kiro MCP server's `list_tools` returns both schemas with `description` required.
- MUST keep the exec-namespace `shell()` function's signature and behaviour unchanged.
  - AC: `inspect.signature(get_all_tools()["shell"])` has no `description` parameter, and
    the exec tool description's generated `shell(...)` line has no `description`.
- MUST run a `shell` call that includes `description` exactly as before, ignoring the
  description, on the native handler and over MCP.
  - AC: the native `shell` handler called with `command="echo hi", description="Say hi"`
    returns `[exit: 0]\nhi`; the same call over MCP returns `isError=False` with `hi`.
- MUST show the description on line 2 of shell and exec TUI tool rows in place of
  `$ <command>` and `python · N lines`, keeping a shell row's ` · in <dir>` suffix and
  line 1 unchanged.
  - AC: `row_text` of a finished shell call with description "Run the CLI tests" and
    `cwd="/workspace/archie-nexus"` is `● Shell · 3 lines · 41ms` / `╰ Run the CLI tests · in archie-nexus`.
  - AC: `row_text` of a finished exec call with description "Count failed requests per host"
    is `● Exec · returned 1 line · 273ms` / `╰ Count failed requests per host`.
- MUST fall back to today's line 2 when `description` is absent, blank, or not a string.
  - AC: existing shell and exec examples in `tests/test_tool_rows.py` pass unchanged, and a
    call with `description="  "` renders like one without.
- MUST render a multi-line description on one line, with whitespace runs collapsed to one space.
  - AC: description `"Run\n  tests"` renders as `Run tests`.
- MUST use the description in place of the command (shell) or the line count (exec) in the
  activity label, the running and finished headline, and the breadcrumb, so subagent status
  lines, the detail header and the web viewer show it.
  - AC: `format_tool_activity("shell", {"command": "git status", "description": "Check the tree"})`
    is `Shell [bold]Check the tree[/]`; for exec it is `Exec [bold]Count rows[/]`.
  - AC: `tool_headline` of a finished shell call with a description contains the
    description and not the command; of a finished exec call is
    `Exec [bold]<description>[/] [dim]<metrics>[/]`; `tool_crumb` returns the description text.
  - AC: a subagent's activity after a shell `tool_call` with a description is
    `Shell <description>`.
- MUST keep the full command and source in the detail view and in a row's copy text.
  - AC: `tool_detail_sections` of a shell call still has a `Command` section with the full
    command; `ToolRow.get_copy_text` still returns the command / source.
- MUST escape Rich markup in the description wherever it goes into markup.
  - AC: description `"list a/[slug]/b"` produces markup that renders the literal brackets
    (checked with `plain_text`).

Out of scope: descriptions on any other tool; changing the `ToolCall` event schema (input is
already a free-form dict); detecting or counting retries; validating descriptions on the
Anthropic/OpenAI/Ollama paths; editing the brain.

## Design

### Overview

The agent adds one required `description` property to the `shell` and `exec` schemas it
sends to the model. Nothing in dispatch changes: the value arrives in the canonical
`tool_call` input and is ignored by the tools. The shared presentation layer gets one
helper, `call_description(name, tool_input) -> str`, which every shell/exec summary uses in
place of the command or line count when it returns non-empty text.

### Code structure

- `agent/src/archie_agent/exec/tool.py`: new `DESCRIPTION_INPUT` (the property schema) and
  `with_description(schema) -> dict` (a copy with `description` first in `properties` and
  added to `required`). `create_registry` builds the exec schema with it.
- `agent/src/archie_agent/exec/native.py`: `make_native_specs` applies `with_description` to
  tools named in a new `DESCRIBED_TOOLS = frozenset({"shell"})`. `native.py` already imports
  from `tool.py` at module level; `tool.py` imports `native` lazily inside `create_registry`,
  so there is no cycle.
- `shared/src/archie_shared/tool_summaries.py`: new public `call_description(name, tool_input)`:
  returns `" ".join(value.split())` for `shell`/`exec` when `tool_input["description"]` is a
  string, else `""`. `format_tool_pending` (shell), `format_tool_activity` (exec) and
  `_format_tool_complete` (shell, both the error and success branches) use it in place of
  the command / `(N lines)`.
- `shared/src/archie_shared/tool_rows.py`: `_shell` and `_exec` set `row.target` to
  `strip_workspace(call_description(...))` when non-empty.
- `shared/src/archie_shared/transcript.py`: `tool_headline`'s exec branch uses the
  description; shell and crumbs pick it up through `tool_summaries`.
- `plans/tui-tool-rows.md`: replace the "no `description` input" rule and intro sentence with
  the description rule and add one shell and one exec example with a description.

The guidance text for `DESCRIPTION_INPUT` (D9):

> What this call does and why, in 5-10 words, for a person watching the session (e.g. "Run
> the CLI tests", "Count failed requests per host"). Describe the purpose, not the command
> or code itself.

## Milestones

### [ ] M1 — Required description in schemas, shown on tool rows

- **Approach:** Add `DESCRIPTION_INPUT`/`with_description` to `exec/tool.py` and apply them to
  the exec schema in `create_registry` and to `shell` in `make_native_specs` (Code structure).
  Add `call_description` to `tool_summaries.py` and use it in `tool_rows._shell`/`_exec`.
  Update the MCP tests that call `shell` (`tests/test_kiro_mcp.py`) to pass a description,
  since the MCP server now validates it. Update `plans/tui-tool-rows.md` (Code structure).
- **Test seam:** `create_registry()` specs and their handlers; `get_all_tools()`;
  `ArchieMcpServer` over a real MCP client (`tests/test_kiro_mcp.py`); `row_text`/`tool_row`
  (`tests/test_tool_rows.py`).
- **Edge cases:** description missing, blank, non-string → today's line 2; multi-line →
  collapsed; contains `/workspace/...` → stripped; a shell call over MCP without a
  description → `isError=True` with an input validation message (documents D1 on Kiro).
- **Deliverable:** the model is told to describe every shell and exec call, and the TUI row
  shows the description.
- **Verify:** `scripts/dev test tests/test_tool_rows.py tests/test_native.py tests/test_exec_tool.py tests/test_kiro_mcp.py tests/test_native_dispatch.py`
  passes, and the full suite has no failures beyond `/tmp/086-baseline-fail.txt`.
- **Outcome:**

### [ ] M2 — Description in every other summary

- **Approach:** Use `call_description` in `format_tool_pending` (shell), `format_tool_activity`
  (exec, `Exec [bold]<desc>[/]`), `_format_tool_complete` (shell error and success branches,
  where `_hi(command)` becomes `_hi(desc or command)`), and `transcript.tool_headline`'s exec
  branch (`Exec [bold]<desc>[/]` running; `Exec [bold]<desc>[/] [dim]<metrics>[/]` done;
  `Exec [bold]<desc>[/] [dim red]<message>[/]` failed). Without a description every output
  stays byte-identical to today.
- **Test seam:** `format_tool_pending`/`format_tool_activity`/`format_tool_complete`
  (`tests/test_tool_summaries.py`); `tool_headline`, `tool_crumb`, `tool_detail_sections`
  and `TranscriptBuilder` subagent activity (`tests/test_transcript_model.py`).
- **Deliverable:** subagent status lines, detail headers, breadcrumbs and web viewer rows
  show the description.
- **Verify:** `scripts/dev test tests/test_tool_summaries.py tests/test_transcript_model.py tests/test_markup.py tests/test_tui_subagents.py tests/test_web_session_detail.py`
  passes, and the full suite has no failures beyond the baseline list.
- **Outcome:**

## Deviations

## Risks

- On Kiro, a call without a description is rejected before it runs (D1). The model
  normally retries at once; the cost is one round-trip per miss.
- Models may still paste the command into `description`. The row then shows the command
  without `$`; readable, and visible in logs if it becomes a pattern.

## Review

- Plan review:
- Code review:
- Merge risk:

## Outcome

PR: pending
CI: pending

## Follow-ups

- Update the brain note `projects/archie/nexus-roadmap/tui-prototype.md`, which says no
  shell or exec call has a `description` input.
