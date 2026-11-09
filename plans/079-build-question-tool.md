# 079 — Question tool and session state

Status: planning
Branch: build-it/079-question-tool (base: main @ f7d3144)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/079-question-tool

## Objective

Skills and the identity prompt tell the agent to ask the user one question at a time, with
options and a recommendation, but the agent can only do that by ending its turn and hoping
the reply fits. Add a root-only `question` tool that blocks the turn until the user picks an
option, types an answer, or dismisses it. Add the minimal live session state (`idle`,
`working`, `blocked` with a kind) that the tool needs to be answerable after a reconnect, in
a shape the later herdr-style status work can build on without a redesign.

## Context

The user wants a question tool like maki's (`_research/maki/plugins/question/`), limited to
one question per call by design. A survey of `_research/` (maki, opencode, codex, openclaw,
hermes, cline, pi) found the same pattern everywhere: the handler registers one pending
request, the client shows a form, a reply over a separate channel wakes the handler, and the
tool returns text. Codex and openclaw keep pending questions in memory only; the persisted
record is the tool call and its result.

Agreed with the user before this build:
- `tool_call` / `tool_result` remain the only persisted record of a question. No new
  persisted event.
- "A question is pending" is live state, never inferred from a `tool_call` without a
  `tool_result` (a crash would leave a stale form).
- The live state uses OSC 7501's vocabulary (`idle | working | done | blocked | error`,
  `blocked` with `kind=permission|question|auth`), from session archie-01m7098x15's herdr
  research, which recommended a canonical session state in `shared/` and noted `blocked`
  needs an ask-user tool to mean anything.
- The state rides on the live-only `session_status` frame (sent on every connect, and
  already re-sent after turns at `harness.py:321` and compaction at `:563`) and on `/status`
  (which already has `turn_active`, `app.py:250`).
- Accepted downsides: the finished answer card keys on the tool name `question`; the answer
  is model-facing text; free-text answers are tool output, not `user_message`; question wait
  time inflates the tool's `duration_ms` in insights.

Codebase facts:
- Tools: `ToolSpec`/`ToolRegistry` in `agent/src/archie_agent/tools.py`. The root registry
  is built in `AgentHarness.__init__` (`harness.py:175-199`); `task` is registered there
  from `agents.py:create_task_tool`. Children call `create_registry()` plus `skill`
  (`agents.py:445-447`), so a tool registered only in the harness is root-only.
- Kiro sessions serve `create_registry()` over MCP (`kiro/harness.py:151`,
  `kiro/mcp_server.py`), excluding `skill` and `task`.
- Tool batches run concurrently and are cancelled on interrupt with a `cancelled` result
  (`loop.py:79-165`).
- `MessageCommand` during an active turn is rejected with `error_notice kind=turn_active`
  (`app.py:383-402`).
- Commands: `shared/src/archie_shared/commands.py`; events: `shared/.../events.py`
  (`SessionStatus` is `forbid_unknown_fields`). Tool row text: `shared/.../tool_summaries.py`
  (used by both the TUI and the web viewer). TUI: `cli/src/archie_cli/tui/app.py`.
- `StatusPayload` in `shared/.../session/descriptor.py` mirrors `/status` but is unused.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check .`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie` is gitignored)
- Failing before this work: `ruff format --check` wants to reformat
  `persona/skills/python/assets/output.py`. Tests: 4706 passed. `ruff check`: passed.
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout, unrelated)
