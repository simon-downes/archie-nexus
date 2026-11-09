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

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | TUI presentation and Esc | Panel docked above the input: question, 2–4 numbered options with descriptions, recommended option preselected and badged; ↑/↓+Enter or 1–4 picks; input stays enabled and Enter with text sends a free-text answer; first Esc dismisses, a later Esc interrupts as today | Modal screen; Esc interrupts with no dismiss | Keeps the conversation visible and the typing habit; the agent learns the question was declined | user Q1 |
| D2 | Prompt changes | Only `persona/prompts/identity.md`'s "ask one question at a time" rule mentions the tool; the tool description says when to use it | Rewrite skill interview steps; no prompt change | Skills inherit the identity rule; Kiro and children lack the tool | user Q2 |
| D3 | Tool input | `question: str` (non-empty), `options: list[{label: str, description?: str}]` with 2–4 items and non-empty labels, `recommended?: int` (0-based index into options). Free text always allowed; single select | Multi-select, multiple questions per call, `allow_custom` flag | User asked for one question per call; maki shape | self |
| D4 | Model-facing result text | Option: `User selected option N: <label>` (N 1-based). Text: `User answered: <text>`. Dismiss: `User dismissed the question without answering. Do not assume an answer.` Invalid input or a second concurrent question: `Error: …`, `is_error=true` | JSON result | Plain text the model reads directly; prefixes let the row summary parse it | self |
| D5 | Answer transport | New strict command `{"type":"answer","question_id":<tool_use_id>, "option"?: int, "text"?: str, "dismiss"?: true}`; exactly one of the three. Unknown/stale `question_id`, or option out of range → targeted `error_notice kind=question_not_pending` / `invalid_answer` | Reusing `message` during a turn; a nested payload | Commands are flat and strict (`commands.py`); keeps the `turn_active` rejection for `message` intact | self |
| D6 | Live state shape | `SessionStatus` gains `state: idle\|working\|blocked\|done\|error` (default `idle`) and `question: PendingQuestion \| None` (default `None`); `PendingQuestion{id, question, options: list[QuestionOption{label, description}], recommended: int\|None}`. `blocked` means kind `question`; only `idle`, `working`, `blocked` are emitted now. `PROTOCOL_VERSION` 3 → 4 | A separate `blocked{kind, …}` object; a new live event type | One frame already sent on connect and turn end; a typed `question` field is the payload herdr-style status can add siblings to. Old clients with `forbid_unknown_fields` drop the frame, so bump the version and warn | self |
| D7 | Where pending state lives | New `agent/src/archie_agent/question.py`: `QuestionBroker` holding at most one pending question and an `asyncio.Future`; the harness owns one, exposes `pending_question`, and emits `SessionStatus` on every change. A second `question` call while one is pending returns an error result | Pending state inside the tool closure; a queue of questions | One question at a time by design; parallel batches can still contain two calls | self |
| D8 | Status construction | `AgentHarness.status_event()` and `KiroHarness.status_event()` build `SessionStatus` (state from `turn_active` + pending question); all existing construction sites in `app.py`, `harness.py`, `kiro/harness.py` call it. `/status` adds `state` and `question`; `StatusPayload` mirrors them | Inline construction at each site | Five sites today would drift | self |
| D9 | Turn start status | The harness emits `state=working` when a root turn starts (after `user_message`) | Only emit on question changes | Otherwise a client connected mid-turn and later clients see stale `idle` until turn end | self |
| D10 | Root-only and Kiro | Registered only in `AgentHarness.__init__`; children (`agents.py:445`) and Kiro (`create_registry()` in `kiro/harness.py:152`) don't get it. Identity wording is conditional ("when you have the `question` tool") because Kiro's prompt includes identity | Kiro support now | Kiro's MCP tools would need the broker wired through ACP; unverified | self |
| D11 | Wait limits | No timeout; interrupt cancels the wait (`cancelled` result from `loop.py`), and the broker clears pending state in `finally` | A timeout | The user may be away; interrupt already ends the wait | self |
| D12 | TUI key routing | `QuestionPanel` widget (new `cli/src/archie_cli/tui/question.py`) above the status bar; `MessageInput` gets an optional `choice_handler` consulted only when its text is empty, for ↑/↓/1–4/Enter. Enter with text sends `answer{text}` instead of `message` while a question is shown | Focusing the panel | Input keeps focus so typing works without a focus switch | self |
| D13 | Completed row | `tool_summaries.py` cases for `question`: pending `Question <question>`; complete `Question <question> → <answer>` from the result prefixes, `(dismissed)` for a dismissal | Generic row | Shared by TUI and web viewer | self |

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check .`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie` is gitignored)
- Failing before this work: `ruff format --check` wants to reformat
  `persona/skills/python/assets/output.py`. Tests: 4706 passed. `ruff check`: passed.
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout, unrelated)

## Requirements

- MUST give the native root agent a `question` tool with the D3 input schema; children and Kiro
  sessions MUST NOT list it.
  - AC: the root `tool_config` contains `question`; a child's `tool_config` and the Kiro MCP
    `list_tools` result do not.
- MUST reject invalid input without blocking: an empty question, fewer than 2 or more than 4
  options, an empty label, or `recommended` out of range.
  - AC: each case returns an `Error:` result with `is_error=true`, and no `session_status`
    with `state=blocked` is emitted.
- MUST block the tool until an answer, and return the D4 text for each answer kind.
  - AC (WebSocket test): after the `tool_call`, clients receive `session_status` with
    `state=blocked` and `question.id == tool_use_id`; sending
    `{"type":"answer","question_id":…,"option":1}` yields a `tool_result` with
    `User selected option 2: <label>`, then `turn_complete`. Same for `text` and `dismiss`.
- MUST expose the pending question as live state: on every connect, on every change, and on
  `/status`.
  - AC: a second client connecting while the question is pending receives it in the connect
    `session_status`; `/status` returns `state: "blocked"` and the question; after the answer a
    `session_status` with `state=working` and `question=null` follows; after the turn,
    `state=idle`.
- MUST emit `state=working` when a root turn starts.
  - AC: the first `session_status` after `user_message` has `state=working`.
- MUST clear pending state when the turn is interrupted while waiting.
  - AC: an `interrupt` during the wait yields a `cancelled` tool result, `turn_interrupted`,
    and a later `session_status` with `question=null`, `state=idle`.
- MUST reject answers that don't match the pending question with a targeted `error_notice`
  and leave the question pending.
  - AC: wrong `question_id` → `kind=question_not_pending`; option out of range →
    `kind=invalid_answer`. A command without exactly one of `option`/`text`/`dismiss` fails to
    decode and is logged and dropped like other malformed commands (`app.py:379`).
- MUST allow only one pending question; a concurrent second call returns an `Error:` result.
  - AC: broker unit test with two concurrent `ask` calls.
- MUST show the pending question in the TUI per D1, including after reconnect.
  - AC (Textual pilot): a `SessionStatus` with a question shows the panel with the recommended
    option highlighted; pressing `2` sends `answer{option=1}`; typing text and Enter sends
    `answer{text}`; Esc sends `answer{dismiss}`; Esc again sends `interrupt`; a
    `SessionStatus` with `question=null` hides the panel.
- MUST render a completed question row per D13 in both the TUI and the web viewer.
  - AC: `format_tool_complete("question", …)` unit tests for the three result kinds.
- MUST update `identity.md` per D2 and the protocol docs for the new command and fields.
  - AC: a prompt test asserts the rule text; `docs/event-spec.md` lists `answer`, `state` and
    `question`.

Out of scope: Kiro support, answering from the web viewer, multi-select, multiple questions per
call, `done`/`error` states and other herdr-style status work, OSC 7501 emission.

## Design

### Overview

A root-only `question` tool asks a `QuestionBroker` to register one pending question and awaits
its future. The harness publishes the pending question in `session_status` and `/status`. The
TUI shows a panel from that frame and replies with a new `answer` command, which `app.py` routes
to the broker. The tool returns plain text, so `tool_call`/`tool_result` stay the only persisted
record.

### Code structure

- `shared/src/archie_shared/events.py`: new `QuestionOption`, `PendingQuestion` structs;
  `SessionStatus.state` and `SessionStatus.question` with defaults.
- `shared/src/archie_shared/commands.py`: new `AnswerCommand` (validation pattern:
  `InterruptCommand.__post_init__`), added to `ClientCommand`.
- `shared/src/archie_shared/protocol.py`: `PROTOCOL_VERSION = 4`.
- `shared/src/archie_shared/session/descriptor.py`: `StatusPayload.state`, `.question`.
- `agent/src/archie_agent/question.py` (new): `QuestionBroker` (`ask`, `answer`, `pending`),
  `create_question_tool(broker)` returning a `ToolSpec` (pattern: `skills.create_skill_tool`),
  input validation and result text.
- `agent/src/archie_agent/harness.py`: owns the broker, registers the tool, adds
  `status_event()` and `answer_question(command) -> ErrorNotice | None`, emits `working` at turn
  start; existing `SessionStatus(...)` sites use `status_event()`. Passes `_tool_use_id` to the
  `question` handler, following the `task` special case at `harness.py:944`.
- `agent/src/archie_agent/kiro/harness.py`: `status_event()` (state from `turn_active`, no
  question); `answer_question` returns `question_not_pending`.
- `agent/src/archie_agent/app.py`: connect and model-switch frames via `status_event()`; routes
  `AnswerCommand`; `/status` fields.
- `cli/src/archie_cli/ws_client.py`: `send_answer(...)`.
- `cli/src/archie_cli/tui/question.py` (new): `QuestionPanel` widget.
- `cli/src/archie_cli/tui/input.py`: optional choice handler for keys on empty input.
- `cli/src/archie_cli/tui/app.py`: composes the panel; handles `SessionStatus.question`;
  routes submit and Esc while a question is shown.
- `shared/src/archie_shared/tool_summaries.py`: `question` cases.
- `persona/prompts/identity.md`, `docs/event-spec.md`, `docs/session-protocol-walkthrough.md`,
  `docs/kiro.md` (question tool not available on Kiro).

### Data flow

1. The model calls `question` → `_execute_tool` → handler → `broker.ask(id, …)` stores the
   pending question and a future → the broker's change callback emits `status_event()`
   (`blocked`, question).
2. The TUI shows the panel → the user picks → `answer` command → `app.py` →
   `harness.answer_question` → the broker validates id and option and resolves the future →
   `ask` clears pending in `finally` and the callback emits `working` → the handler returns text
   → normal `tool_result`.
3. Interrupt → `loop.py` cancels the worker → `CancelledError` in `ask` → `finally` clears
   pending and emits status → `cancelled` result. Turn end emits `idle`.
4. Reconnect → the connect `status_event()` carries the pending question → the panel reappears.

### Error handling

- A status emission failure is logged and ignored (pattern: best-effort status at
  `harness.py:328`); the question still waits and `/status` still shows it.
- An answer for a stale id → targeted `question_not_pending`; nothing changes.
- A TUI send failure → client error line; the panel stays.

## Milestones

### [ ] M1 — Agent answers a question end to end over the WebSocket

- **Approach:** shared structs and command, protocol bump, `question.py`, harness wiring,
  `app.py` routing and `/status`, Kiro `status_event`.
- **Test seam:** `starlette.testclient` WebSocket against `archie_agent.app` with a mocked LLM
  (pattern: `tests/test_ws_integration.py:206`); `QuestionBroker` unit tests; `decode_command`
  tests.
- **Edge cases:** invalid input → error result, no blocked status; second concurrent ask →
  error; interrupt while waiting → cancelled and cleared; wrong id → `question_not_pending`.
- **Deliverable:** a WebSocket client can see, answer and dismiss a question; `/status` shows it.
- **Verify:** `scripts/dev test tests/test_question_tool.py tests/test_ws_integration.py -q`
  passes, then the full suite has no new failures.
- **Outcome:**

### [ ] M2 — TUI question panel

- **Approach:** `QuestionPanel` above the status bar; the `SessionStatus` handler shows and
  hides it; input stays enabled while it is shown; keys per D12; Esc per D1. The panel hides
  locally when an answer is sent.
- **Test seam:** Textual `app.run_test` pilot with a stubbed `WSClient` (pattern:
  `tests/test_tui_window.py`).
- **Deliverable:** the D1 behaviour in the TUI.
- **Verify:** `scripts/dev test tests/test_tui_question.py -q` passes.
- **Outcome:**

### [ ] M3 — Row summaries, prompt and docs

- **Approach:** D13 summary cases; D2 identity text; docs per Code structure.
- **Test seam:** `format_tool_pending`/`format_tool_complete`; `build_system_prompt_structured`.
- **Deliverable:** finished rows read as question → answer; the agent is told to use the tool.
- **Verify:** `scripts/dev test tests/test_tool_summaries.py tests/test_prompt.py -q` passes,
  and `scripts/dev ruff check .` passes.
- **Outcome:**

## Deviations

## Risks

- Old CLIs (protocol 3) reject the extended `session_status` and lose model and branch display
  until upgraded; the handshake mismatch warning tells them.
- Tool duration includes user wait time and inflates insights for `question` (accepted).
- Kiro's prompt includes identity, which mentions a tool Kiro lacks; the wording is conditional.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Kiro support for `question`.
- herdr-style `done`/`error` states and OSC 7501 emission.
