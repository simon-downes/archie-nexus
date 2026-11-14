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
