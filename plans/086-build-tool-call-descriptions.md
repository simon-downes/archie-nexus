# 086 — Descriptions on shell and exec calls

Status: building
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
  reads only `source`. The exec spec's own handler, `_exec_handler(source, **kwargs)`, forwards
  extra kwargs to `run_exec`, which would raise `TypeError` on `description`; nothing calls it
  with model input today, but it is changed to drop `description`.
- Kiro tests: `tests/fake_kiro.py` calls `shell` over the real MCP server without a
  description (lines ~239 and ~272), and `tests/test_kiro_harness.py::test_turn_with_archie_tool`
  asserts the canonical input equals `{"command": "echo KIRO-OK"}`.
- Web viewer rows are `tool_headline` plus `tool_preview` (`transcript.py`); for shell the
  preview skips the command's first line because the headline shows it.
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
  `ruff check` passes. Failing in `test_question_tool.py`: test_answer_without_pending_question_is_rejected,
  test_dismiss_answer, test_interrupt_while_waiting_clears_question, test_manual_compaction_emits_working,
  test_option_answer_round_trip, test_reconnect_and_status_show_pending_question,
  test_root_llm_is_offered_the_question_tool, test_stale_or_out_of_range_answers_are_rejected,
  test_text_answer, test_turn_start_emits_working. In `test_ws_integration.py`:
  test_compact_command_runs_manual_compaction, test_compact_command_rejected_while_turn_active,
  test_concurrent_message_rejection_is_targeted, test_events_complete_log_after_turn,
  test_events_on_start_has_only_session_started, test_events_unknown_cursor_returns_409,
  test_events_valid_cursor_slices_after, test_existing_session_does_not_duplicate_session_started,
  test_status_includes_session_metadata, test_two_clients_receive_identical_completed_turn,
  test_websocket_handshake_on_connect, test_websocket_message_and_events, test_websocket_tool_turn.
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
| D6 | Display text | Whitespace collapsed to one line and otherwise shown as written (no `/workspace` stripping); blank or non-string counts as missing | Truncate to N words; strip `/workspace` like other targets; treat a description equal to the command as missing (maki) | Rows already cut to width. `strip_workspace` is for paths and turns a bare `/workspace` in prose into `.`. The schema text tells the model not to repeat the command, and a repeat would still be readable | self |
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
  - AC: `call_description("shell", {"description": "Run\n  tests"})` is `Run tests`, and
    `row_text` of that call shows `╰ Run tests`.
- MUST show the description in the activity label, the headline and the breadcrumb, so
  subagent status lines, the detail header and the web viewer show it: for shell in place
  of the command; for exec in place of `(N lines)` in the activity label, and after the
  `Exec` label in the headline (which shows no line count today).
  - AC: `format_tool_activity("shell", {"command": "git status", "description": "Check the tree"})`
    is `Shell [bold]Check the tree[/]`; for exec with description "Count rows" it is `Exec [bold]Count rows[/]`.
  - AC: `tool_headline` of a finished shell call with a description contains the
    description and not the command; of a running exec call is `Exec [bold]<description>[/]`;
    of a finished exec call is `Exec [bold]<description>[/] [dim]<metrics>[/]`.
  - AC: `tool_crumb` of a shell call with description "Check the tree" is `Shell Check the tree`
    (the label stays, as for every other tool).
  - AC: `plain_text` of a subagent's activity after a shell `tool_call` with a description is
    `Shell <description>`.
- MUST keep the web viewer's shell row showing the command when the headline shows a
  description.
  - AC: `tool_preview` of a shell call with a description starts at the command's first
    line; without one it still starts at the second.
- MUST keep the full command and source in the detail view and in a row's copy text.
  - AC: `tool_detail_sections` of a shell call with a description still has a `Command`
    section with the full command, and no `Options` section unless another option (`cwd`,
    `timeout`) is set; `ToolRow(tool).get_copy_text()` still returns the command / source
    (`tests/test_tui_copy_paste.py`).
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
  so there is no cycle. `_exec_handler` takes `description: str | None = None` and drops it.
- `shared/src/archie_shared/tool_summaries.py`: new public `call_description(name, tool_input)`:
  returns `" ".join(value.split())` for `shell`/`exec` when `tool_input["description"]` is a
  string, else `""`. `format_tool_pending` (shell), `format_tool_activity` (exec) and
  `_format_tool_complete` (shell, both the error and success branches) use it in place of
  the command / `(N lines)`.
- `shared/src/archie_shared/tool_rows.py`: `_shell` and `_exec` set `row.target` to
  `call_description(...)` when non-empty (D6: not passed through `strip_workspace`).
- `shared/src/archie_shared/transcript.py`: `tool_headline`'s exec branch uses the
  description; shell and crumbs pick it up through `tool_summaries`. `tool_preview` keeps a
  shell command's first line when `call_description` is non-empty. `tool_detail_sections`
  leaves `description` out of a shell call's `Options` section (the header already shows
  it), so a call with only `command` and `description` has no `Options` section.
- `tests/fake_kiro.py`: its `shell` calls pass a `description`;
  `tests/test_kiro_harness.py` asserts it in the canonical input.
- `plans/tui-tool-rows.md`: replace the "no `description` input" rule and intro sentence with
  the description rule, and add the two `row_text` examples from the Requirements ACs (shell
  "Run the CLI tests" in `archie-nexus`, exec "Count failed requests per host") so the doc
  and `tests/test_tool_rows.py` stay in step.

The guidance text for `DESCRIPTION_INPUT` (D9):

> What this call does and why, in 5-10 words, for a person watching the session (e.g. "Run
> the CLI tests", "Count failed requests per host"). Describe the purpose, not the command
> or code itself.

## Milestones

### [x] M1 — Required description in schemas, shown on tool rows

- **Approach:** Add `DESCRIPTION_INPUT`/`with_description` to `exec/tool.py` and apply them to
  the exec schema in `create_registry` and to `shell` in `make_native_specs` (Code structure).
  Add `call_description` to `tool_summaries.py` and use it in `tool_rows._shell`/`_exec`.
  Update every MCP client that calls `shell` to pass a description, since the MCP server
  now validates it: `tests/test_kiro_mcp.py` and `tests/fake_kiro.py` (with the input
  assertion in `tests/test_kiro_harness.py`). Make `_exec_handler` drop `description`. Update `plans/tui-tool-rows.md` (Code structure).
- **Test seam:** `create_registry()` specs and their handlers; `get_all_tools()`;
  `ArchieMcpServer` over a real MCP client (`tests/test_kiro_mcp.py`); `row_text`/`tool_row`
  (`tests/test_tool_rows.py`).
- **Edge cases:** description missing, blank, non-string → today's line 2; multi-line →
  collapsed; contains `/workspace` → shown as written; a shell call over MCP without a
  description → `isError=True` with an input validation message (documents D1 on Kiro).
- **Deliverable:** the model is told to describe every shell and exec call, and the TUI row
  shows the description.
- **Verify:** `scripts/dev test tests/test_tool_rows.py tests/test_native.py tests/test_exec_tool.py tests/test_kiro_mcp.py tests/test_kiro_harness.py tests/test_native_dispatch.py`
  passes, and the full suite has no failures beyond the Baseline list.
- **Outcome:** `with_description`/`DESCRIPTION_INPUT` in `exec/tool.py`, applied to exec and
  (via `DESCRIBED_TOOLS`) to native shell; `_exec_handler` drops `description`;
  `call_description` in `tool_summaries.py` drives `tool_rows._shell`/`_exec`. New tests in
  `test_exec_tool.py` (schemas, exec-namespace signature, handlers ignore it),
  `test_tool_rows.py` (description rows, fallback, collapsing, cutting) and `test_kiro_mcp.py`
  (required over MCP; a call without one is rejected). Kiro fake and harness test pass a
  description. Verify command: 168 passed. Full suite: the same 23 baseline failures, no new
  ones; `ruff check` and `ruff format --check` clean.

### [ ] M2 — Description in every other summary

- **Approach:** Use `call_description` in `format_tool_pending` (shell), `format_tool_activity`
  (exec, `Exec [bold]<desc>[/]`), `_format_tool_complete` (shell error and success branches,
  where `_hi(command)` becomes `_hi(desc or command)`), and `transcript.tool_headline`'s exec
  branch (`Exec [bold]<desc>[/]` running; `Exec [bold]<desc>[/] [dim]<metrics>[/]` done;
  `Exec [bold]<desc>[/] [dim red]<message>[/]` failed). Without a description every output
  stays byte-identical to today. In `transcript.tool_preview`, return all of a shell
  command's preview lines when there is a description, and in `tool_detail_sections` skip
  `description` in the shell `Options` section. With a description the exec headline
  label is plain `Exec` with the description in bold, matching `Shell [bold]…[/]`; that
  differs from the bold `[bold]Exec[/]` of a description-less row, and that is intended.
- **Test seam:** `format_tool_pending`/`format_tool_activity`/`format_tool_complete`
  (`tests/test_tool_summaries.py`); `tool_headline`, `tool_crumb`, `tool_detail_sections`
  `tool_preview` and `TranscriptBuilder` subagent activity (`tests/test_transcript_model.py`);
  `ToolRow.get_copy_text` (`tests/test_tui_copy_paste.py`).
- **Deliverable:** subagent status lines, detail headers, breadcrumbs and web viewer rows
  show the description.
- **Verify:** `scripts/dev test tests/test_tool_summaries.py tests/test_transcript_model.py tests/test_markup.py tests/test_tui_subagents.py tests/test_web_session_detail.py tests/test_tui_copy_paste.py`
  passes, and the full suite has no failures beyond the baseline list.
- **Outcome:**

## Deviations

## Risks

- On Kiro, a call without a description is rejected before it runs (D1). The model
  normally retries at once; the cost is one round-trip per miss.
- Models may still paste the command into `description`. The row then shows the command
  without `$`; readable, and visible in logs if it becomes a pattern.

## Review

- Plan review: one pass, 2 ❌ 2 ⚠️ 5 ℹ️, all accepted and fixed: Kiro fake client and
  harness assertion added to M1 (❌); web viewer `tool_preview` behaviour decided and added to
  M2 (❌); crumb and exec-headline ACs corrected (⚠️); `_exec_handler` drops `description`,
  baseline IDs moved into the plan, `test_kiro_harness.py` added to M1 Verify, subagent and
  multi-line ACs name their boundary (ℹ️). Second pass (for the ❌ fixes) confirmed both
  fixes and raised 1 ⚠️ 4 ℹ️, all accepted and fixed: shell `Options` drops `description`
  (⚠️); copy-text AC gets a milestone and test file; the new doc examples are the two
  `row_text` ACs; exec headline styling change noted as intended; D6 now leaves
  descriptions unstripped, since `strip_workspace` turns a bare `/workspace` into `.`.
- Code review:
- Merge risk:

## Outcome

PR: pending
CI: pending

## Follow-ups

- Update the brain note `projects/archie/nexus-roadmap/tui-prototype.md`, which says no
  shell or exec call has a `description` input.
