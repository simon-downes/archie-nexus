# 078 — Tool and subagent detail views in the TUI and web viewer

Status: reviewing
Branch: build-it/078-tool-detail-views (base: main @ 245b73f)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/078-tool-detail-views

## Objective

Make tool calls and subagents inspectable in both clients, and keep the TUI fast in long
sessions. Clicking a tool call in the TUI or the web session viewer opens an overlay with
its full, syntax-highlighted input and result. Clicking a subagent opens its transcript,
whose tool calls are clickable in the same way. Escape backs out one level, and a
breadcrumb title shows where you are. Iteration blocks get a permanent dim border so
parallel tool calls are visible at a glance. Underneath, one widget-free transcript model
in `shared/` replaces the two reducers; the TUI mounts only a bounded window of it, renders
tool rows as single widgets and streams Markdown incrementally.

## Context

The user's request: "clicking on a tool call in the tui (or web client) should show the
tool call and the result in a pop up/over window. this will be great for debugging and
general visibility. we can remove the existing click to expand on exec call and just show
the first 10 lines. we can also do that for shell tool (which i think always shows the
full command). bonus points if we can add syntax highlighting when viewing in the popup.
clicking a subagent should show the transcript. tool calls in the subagent view should
also be clickable the same as in the main session window. esc should back out of popup
views. might be useful to have a breadcrumb or similar title bar structure to indicate
what we're viewing." Plus: "i like the idea of a dim border on iteration blocks". The web
client stays viewer-only (no session driving).

Codebase facts:

- TUI: `cli/src/archie_cli/tui/conversation.py` has `IterationBlock` (one per LLM
  response that calls tools, focusable, `IterationBlock:focus` border in `archie.tcss`)
  and `ToolEntry` (non-focusable; exec source collapsed to 10 lines with click to
  expand). `ToolEntry` keeps only the pending summary string, not the raw input or result.
- TUI subagents: `app.py::_handle_scoped_event` folds child events into
  `ChildActivityState` (`subagents.py`), keeping only the last 3 activity lines.
  `SubagentActivity` rows have no click handler. `SubagentScreen` (a `ModalScreen`) shows
  status plus those lines and is opened only from the command palette
  (`models_provider.py::SubagentProvider` → `app.open_child_detail`).
- Web viewer: `orchestrator/src/archie_orchestrator/transcript.py::TranscriptBuilder`
  reduces events into nodes rendered by `templates/viewer/_nodes.html`; live updates go
  over SSE (`web.py::session_live`, `static/viewer/live.js`, htmx swaps). Tool rows are
  `<details>` that expand inline to input and result; subagent events are nested inline
  under their `task` tool. `IterationStart` is ignored, so iterations are not grouped.
  Pygments highlighting already exists (`highlight_code`).
- `shared/src/archie_shared/tool_summaries.py` produces Rich-markup summaries used by
  both clients; `format_tool_pending("shell")` contains the full command.
- Performance (brain entry `projects/archie/nexus-tui-performance.md`, headless benchmark
  on the 5,974-event WAF session `~/.nexus/sessions/tillo-platform-01m61zwmar.jsonl`):
  8,905 live widgets, 238.7 ms per keypress in a sibling `Input` versus 69.7 ms for an
  empty app, 27 s full replay. Keeping only the last 200 `Conversation` children brought
  keypress cost to 69.9 ms. `MarkdownStream.write` cut the last-10-tick streaming cost of
  the longest reply (9,802 chars) from 163 ms to 68 ms. Tau
  (`/workspace/_research/tau/src/tau_coding/tui/widgets.py`) windows 200 items, page 80, overscan 40.
- Web viewer full-page render of the WAF log takes 0.52–0.61 s and produces a 9.0 MB page.
- Textual 8.2.8 provides `Markdown.get_stream()` → `MarkdownStream` (async `write`, `stop`).
- Viewer pages send `VIEWER_CSP` (`script-src 'self'`): scripts must be static files with
  no inline handlers.
- Existing TUI tests reach into `ArchieApp` internals (`_child_activity`,
  `_transient_assistant_text`, `conv.add_*` mocks) that this build replaces.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (the environment is `.venv-archie`, gitignored)
- Failing before this work: none (2552 passed; ruff clean)
- Not included: user's uncommitted changes in `plugins/datadog/` (README.md, __init__.py,
  formatting.py, logs.py, skills/datadog/SKILL.md, tests/test_datadog_monitors.py,
  tests/test_datadog_reads.py, validation.py) and untracked
  `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Scope | Everything discussed: popups, subagent transcript, breadcrumbs, dim iteration border, 10-line exec/shell rows, a shared transcript reducer in `shared/` used by both clients, TUI windowing, `MarkdownStream`, cheaper tool rows | Interaction features only, with separate reducers | The user wants the full set in one build | user Q1 |
| D2 | Assistant Markdown renderer | Keep Textual `Markdown` for assistant messages | Rich Markdown in one `Static` | Windowing bounds the cost; keeps fences, tables, links and theme colours | user Q2 |
| D3 | Shape of the shared model | A widget-free, HTML-free node tree in new `shared/src/archie_shared/transcript.py` (turns → items; iteration groups → tools; tools → subagents → items), reducing events into `Change` records (`append`/`replace`/`append_text`/`remove`), with an id index and parent links | Each client keeps its own reducer; a flat display list in `shared/` | The web viewer's `TranscriptBuilder` already has a change protocol the SSE relay depends on; generalising it keeps the web path intact and gives the TUI the same event rules. A tree fits the web's nested DOM; the TUI flattens it | self |
| D4 | What stays outside the model | TUI status-bar accounting (cumulative tokens, context %, model key), throbber, input enable/disable, `ErrorNotice` client errors and `!` shell output stay in `ArchieApp` | Moving accounting into the model | They are client state, not transcript; the status bar also counts child requests, which the transcript's per-turn usage does not | self |
| D5 | How each client renders a node | Nodes carry data only; the web renders them through Jinja macros plus template globals in `web.py`; the TUI maps nodes to widgets in a new `cli/src/archie_cli/tui/transcript_view.py` | HTML/markup properties on shared nodes (as today's `ToolNode.summary_html`) | `shared/` must not depend on markupsafe, Pygments or Textual | self |
| D6 | TUI tool row widgets | One `Static` per tool row (`ToolRow`) and per subagent row (`SubagentRow`) inside an `IterationBlock` container, with no child widgets | One `Static` per iteration with `@click` link markup; today's `ToolEntry` with 2–3 children | A whole-row click target survives wrapping; per-line link markup only reacts on characters. Still removes most tool widgets (each `ToolEntry` today has 2–3 children) | self |
| D7 | Iteration border | Permanent dim left border on every iteration block in both clients (TUI `$primary 30%`-style muted colour, web `--v-chrome-border`); TUI keeps a brighter border on focus | Count badge ("3 parallel") | User asked for the dim border | user (prior turn) |
| D8 | Exec and shell rows | Row shows the summary line plus the first 10 lines of exec source or shell command, then `… +N lines`; no click-to-expand; full text is in the popup. Same in both clients | Keep exec click-to-expand | User request | user (prior turn) |
| D9 | TUI windowing parameters | Window 200 top-level rows, shift page 80, overscan 40, boundary rows showing hidden counts, anchor-preserving shift; replay builds the model then mounts only the window | Mount everything; virtual scrolling of lines | Tau's tested numbers; the WAF measurement showed a 200-child window returns typing to baseline | self |
| D10 | TUI streaming | `StreamingMessage` writes fragments through `Markdown.get_stream()` (`MarkdownStream`), no 0.1 s full-text throttle; when the final text equals the streamed text the widget is finalised in place | Keep throttled `Markdown.update` | Measured 163 ms → 68 ms per tick on the longest WAF reply | self |
| D11 | TUI copy and focus | Keep focusable message blocks and `ctrl+c` copy of the focused block; tool and subagent rows are not focusable | Selection-based copy (Tau) | Windowing bounds focusables to ≤ 240 rows; changing copy UX was not requested | self |
| D12 | TUI popups | `ModalScreen` subclasses in new `cli/src/archie_cli/tui/detail.py`: `ToolDetailScreen` and a rewritten `SubagentScreen` sharing a `DetailScreen` base with a breadcrumb header and an `escape` binding that dismisses; the subagent screen hosts the same windowed transcript widget | One screen with internal navigation | Textual's screen stack gives Esc-backs-one-level and breadcrumb-from-stack for free | self |
| D13 | Web overlays | Native `<dialog>` elements rendered in place: each tool has a row button and a dialog with its detail; each subagent has a row button and a dialog holding its items container. A static `static/viewer/overlay.js` opens them with `showModal()`, keeps the open stack, writes the breadcrumb, and reopens a dialog whose element a live swap replaced | Fetch detail fragments from new routes | Live SSE swaps target ids anywhere in the document, so subagent dialogs update live with no new route or cache; native dialog gives Esc and stacking; CSP allows a static script | self |
| D14 | Syntax highlighting source | Shared `tool_detail_sections(tool)` in `archie_shared/transcript.py` returns sections (title, text, language hint, filename hint); the TUI renders with Rich `Syntax`, the web with the existing Pygments `highlight_code` | Per-client lexer choice | One rule for which text is Python, Bash, JSON or a file type | self |
| D16 | `ctrl+x` stop-child target | The topmost open `SubagentScreen`'s subagent; dismissing it falls back to the next `SubagentScreen` below, else no target. Palette and row click open the same screen | Keep `_active_child_key` set only from the palette | Clicking is now the main way to open a subagent; the target must follow what the user is looking at | self |
| D17 | Popup size cap | Both clients cap each detail section with `preview()` / `PREVIEW_CHARS` (8,000 chars) and show the omitted count | Uncapped in the TUI | A 32 KB `read` result through Rich `Syntax` would stall the TUI; matches the web cap | self |
| D18 | Where subagent rows sit | Directly under their `task` tool row inside the iteration block (tool row, its subagent rows, next tool row) | At the end of the block | Mirrors the model (`ToolNode.children`) and today's TUI placement | self |
| D19 | What creates an iteration node | The first `ToolCall` (or orphan `ToolResult`) for a group key; `IterationStart` creates nothing | Create on `IterationStart` | Text-only iterations must not leave empty blocks (today's `app.py:478` rule) | self |
| D15 | Content-filtered turns | The model emits a turn-error notice for `stop_reason == "content_filtered"` (root and child); the message constants move to `archie_shared/transcript.py` | Keep the TUI-only local error row | Both clients then show it; the web currently shows nothing | self |

## Requirements

Shared model
- MUST reduce canonical events into one transcript model in `archie_shared.transcript` used by both the TUI and the web viewer.
  - AC: `cli/` and `orchestrator/` contain no other event-to-transcript reducer (`TranscriptBuilder` is imported from `archie_shared.transcript`; `ArchieApp._handle_scoped_event` and `ChildActivityState` are gone).
  - AC: `tests/test_transcript_model.py` covers turns, streaming reconciliation, iteration grouping, subagent nesting and status, compaction, errors, dedup by id, and orphan tool results.
- MUST group tool calls by `(scope, subagent_index, turn, iteration)`.
  - AC: two tool calls in one iteration produce one iteration node with two tools; calls in consecutive iterations produce two nodes.

Rows
- MUST show a permanent dim left border on every iteration block in both clients.
  - AC (TUI): an unfocused `IterationBlock` has a non-transparent left border. AC (web): tools of one iteration are inside one `.iteration` element styled with a left border.
- MUST show exec source and shell commands as at most 10 lines in the row, followed by `… +N lines` when longer, with no click-to-expand.
  - AC: a 25-line exec source renders 10 source lines and `+15 lines` in both clients (web: M2; TUI: M3). Clicking the TUI row opens the popup instead of expanding (M4).

Tool popups
- MUST open a popup showing the tool's name, status, duration, input and result when a tool row is clicked, in the main view and inside a subagent view.
  - AC (TUI): clicking a `ToolRow` pushes `ToolDetailScreen`, whose text includes the full input and result. AC (web): each tool row is a button whose dialog contains the input and the result, each up to `PREVIEW_CHARS` with an omitted count (D17).
- MUST syntax-highlight popup input and result where a language is known: exec source as Python, shell command as Bash, JSON-shaped input and results as JSON, edit results as diffs, and `write`/`edit` content by file extension (`read` results stay plain; see Deviations).
  - AC: `tool_detail_sections` returns the expected language hints; the web dialog contains Pygments spans; the TUI section uses `rich.syntax.Syntax`.
- MUST update an open tool popup when its result arrives.
  - AC (TUI): a popup opened on a pending tool shows the result after the `ToolResult` is applied. AC (web): with a tool dialog open, swapping in a replacement tool fragment through `htmx.swap` (as `live.js` does) leaves the new dialog open (browser check, M2).

Subagents
- MUST show one clickable row per subagent under its `task` tool, with agent name, index, status and cost; clicking opens the subagent's transcript.
  - AC (TUI): clicking a `SubagentRow` pushes `SubagentScreen` whose transcript contains the child's assistant text and tool rows. AC (web): the subagent row button opens a dialog containing the child's items.
- MUST make tool rows inside a subagent transcript open the same tool popup.
  - AC: clicking a tool row inside `SubagentScreen` pushes `ToolDetailScreen`; in the web, a tool dialog opens from inside a subagent dialog.
- MUST update an open subagent view live as child events arrive.
  - AC (TUI): applying a child `ToolCall` while `SubagentScreen` is open adds a row to it. AC (web): child item fragments target ids inside the subagent dialog (existing live tests adjusted).

Navigation
- MUST close only the top popup on Escape, without interrupting the agent.
  - AC (TUI): pressing `escape` in `ToolDetailScreen` over `SubagentScreen` leaves `SubagentScreen` open and sends no `InterruptCommand`. AC (web): native dialog cancel closes the top dialog (browser check).
- MUST show a breadcrumb in every popup header naming each level.
  - AC: TUI header reads like `Session › general #0 › Shell ls`; web dialog header lists the same levels from the open stack.
- MAY let a click on a breadcrumb segment close the levels above it (web).

Performance (TUI)
- MUST mount at most 240 top-level transcript rows (window 200 + overscan 40) regardless of history length, while the model keeps every item.
  - AC: a test that replays 1,000 synthetic turns finds ≤ 240 rows mounted in the conversation and all turns in the model.
- MUST let the user scroll back through the whole history: reaching the top boundary mounts the previous page and keeps the previously first visible row in place; reaching the bottom of a shifted window mounts the next page; sending a message returns to the latest window.
  - AC: tests drive `scroll_home`/`scroll_end` and assert the window start/end move by 80 and the anchor row is still mounted.
  - AC: after scrolling back, applying a new root `UserMessage` leaves the window end at the last row and the view scrolled to the end.
- MUST mount the window once at the end of a full replay rather than per event.
  - AC: the WAF benchmark replay (`/tmp/tui_bench_078.py`, new in M5, adapted from the brain entry's script) finishes in under 10 s (baseline 26.8–28.7 s) and leaves under 2,500 live widgets (baseline 8,905), with keypress cost under 110 ms (baseline 238.7 ms, empty app 69.7 ms). Manual check, recorded in M5 Outcome.
- MUST keep the per-delta streaming cost from growing with reply length: render only the new fragment on each delta, not the whole reply (D10).
  - AC: a test streams 50 deltas and finds the final rendered Markdown source equal to the joined text, with no full re-render (`Markdown.update`) during streaming.
  - AC: `/tmp/078-stream-bench.py` (new in M6) streams the longest WAF reply (9,802 chars) in 80-char deltas; the average of the last 10 ticks is under 100 ms (baseline 163 ms; prototype 68 ms). Recorded in M6 Outcome.

Compatibility
- MUST keep existing behaviour not named above: turn status line, compaction rows, error and cancelled rows, client error rows, `!` shell output, model switching, `ctrl+s` subagent picker, `ctrl+x` stop child, reconnect with `?after=` cursor and 409 full reset, web live SSE resume.
  - AC: the existing test suites for these pass, adjusted only where they assert removed internals.
- MUST NOT change the canonical event or WebSocket schemas.
  - AC: `shared/src/archie_shared/events.py` and `commands.py` are unchanged.

Out of scope: Rich Markdown for assistant messages (D2); selection-based copy (D11); a web client that sends commands; persisting or exporting the transcript model; keyboard navigation into tool rows.

## Design

### Overview

`archie_shared.transcript.TranscriptBuilder` becomes the only reducer. Each client feeds it
events and applies the returned `Change`s: the web relay renders them as htmx fragments (as
today), and the TUI's `TranscriptView` maps them to widgets inside a bounded window. Popups
are a Textual screen stack in the TUI and a `<dialog>` stack in the web viewer.

### Code structure

- `shared/src/archie_shared/transcript.py` (new): node dataclasses, `Change`, `TranscriptBuilder`, `build`, `dom_id`, `preview`, `PREVIEW_CHARS`, `source_preview(text, limit=10)`, `tool_detail_sections(tool)`, `CONTENT_FILTERED*` constants. Starts as a move of `orchestrator/.../transcript.py`'s builder (lines 161–560) minus HTML.
- `orchestrator/src/archie_orchestrator/transcript.py`: keeps Markdown, Pygments and `markup_to_html` helpers; gains HTML render helpers for nodes (`tool_summary_html`, `tool_detail_html`, …) registered as Jinja globals in `web.py`.
- `orchestrator/.../templates/viewer/_nodes.html`, `static/viewer/viewer.css`, `static/viewer/overlay.js` (new), `templates/viewer/detail.html` (loads overlay.js always), `web.py::render_change` (maps replace by node kind: turn → footer, tool → head, subagent → row).
- `cli/src/archie_cli/tui/transcript_view.py` (new): `TranscriptView(VerticalScroll)` (windowed, used by the main conversation and by `SubagentScreen`), row widgets (`IterationBlock`, `ToolRow`, `SubagentRow`, `WindowBoundary`), node→widget factory.
- `cli/src/archie_cli/tui/detail.py` (new): `DetailScreen`, `ToolDetailScreen`, `SubagentScreen`.
- `cli/src/archie_cli/tui/conversation.py`: keeps message widgets; `Conversation` becomes a `TranscriptView` subclass (keeps id `#conversation`); `ToolEntry` and the old `IterationBlock` are removed.
- `cli/src/archie_cli/tui/subagents.py`: removed (contents replaced by `detail.py` and `transcript_view.py`); `models_provider.py::SubagentProvider` reads `app.transcript.subagents`.
- `cli/src/archie_cli/tui/app.py`: `_render_canonical` = dedup → accounting → `self.transcript.apply(event)` → `self._dispatch_changes(changes)` → client side effects.

### Data model (shared)

All nodes are dataclasses with `id: str` and `kind: str`.
- `TurnNode`: `number:int`, `user:TextNode|None`, `items:list`, `usage:UsageTotals` (root requests), `duration_ms:int` (root request durations), `stop_reason:str|None`; slot ids `items_id`, `footer_id`.
- `TextNode`: `role:"user"|"assistant"`, `content:str`, `interrupted:bool`.
- `StreamNode`: `text:str`.
- `IterationNode` (new): `scope:str|None`, `subagent_index:int|None`, `turn:int`, `iteration:int`, `tools:list[ToolNode]`; slot id `tools_id`.
- `ToolNode`: `tool_use_id`, `name`, `input:dict`, `status:"pending"|"ok"|"error"`, `result:str|None`, `duration_ms`, `result_lines`, `result_bytes`, `children:list[SubagentNode]`; slot ids `children_id`, `head_id`.
- `SubagentNode`: `scope`, `index`, `agent:str` (from the parent task input `tasks[index].agent`, default `"child"`), `status:"running"|"complete"|"error"|"interrupted"`, `cost_usd:float` (direct child requests), `context_tokens:int`, `error:str`, `items:list`; slot ids `items_id`, `row_id`.
- `CompactionNode` (replaces compaction `NoticeNode`s): `request_id`, `reason`, `status:"pending"|"success"|"failure"|"interrupted"`, `marker:bool` (legacy `ContextCompacted`), `tokens_before`, `tokens_after:int|None`, `duration_ms`, `error:str|None`, `estimated_next_request_cost_usd:float|None`, `cost_saved_usd:float`, `summary:str`.
- `NoticeNode`: `variant:"turn-error"|"turn-interrupted"`, `title`, `detail`.
- `DetailSection`: `title:str`, `text:str`, `language:str|None`, `filename:str|None`, `omitted:int`.
- `TranscriptBuilder.nodes: dict[str, node]`, `.container_of: dict[node_id, slot_id|None]`, `.owner_of: dict[slot_id, node_id]`, `.subagents: dict[(scope, index), SubagentNode]`, `.tools: dict[tool_use_id, ToolNode]`; lifetime = one client view of one session; the TUI replaces it on a 409 reset.

### Data flow

- Web page load: `build(events)` → template renders the tree (unchanged path).
- Web live: `builder.apply(event)` → `render_change` per change → SSE fragment (unchanged path); replace targets now depend on node kind.
- Web live, compaction: the shared builder handles `CompactionStart` (pending `CompactionNode`) and `CompactionComplete` replaces it, so the web viewer now shows pending compactions too.
- TUI event: `ArchieApp._render_canonical` → `transcript.apply` → `_dispatch_changes` sends every change to the main `Conversation` and to each open `SubagentScreen`/`ToolDetailScreen`. A view handles `append` whose parent slot is one of its root slots (main: `None` and every turn's `items_id`; subagent screen: that subagent's `items_id`) by appending a row; any other change refreshes the nearest mounted widget registered for the node or an ancestor (`container_of`/`owner_of` walk). Rows not mounted are rendered from node state when they enter the window.
- TUI replay: `view.suspend()` before history, `view.resume()` after → one window mount.
- Failure paths: a change for an unknown node or unmounted widget is ignored; a popup for a tool that a 409 reset removed is dismissed by `_reset_replay_state` (pops all detail screens).

### Non-functional

- TUI mounted rows ≤ 240; replay mounts once; streaming appends fragments.
- Web: full-page render of the WAF log stays under 1.0 s and 13.5 MB (baseline 0.52–0.61 s, 9.0 MB), measured with `/tmp/078-webbench.py` (exists) in M2.

## Milestones

### [x] M1 — prefactor: shared transcript model; web viewer groups iterations

- **Approach:** Move `TranscriptBuilder` and nodes from `orchestrator/src/archie_orchestrator/transcript.py` into new `shared/src/archie_shared/transcript.py`, removing HTML properties (`summary_html`, `detail_html`, `input_html`, `result_preview`, `TextNode.html`, `NoticeNode.body_html`). Add `IterationNode` (created by D19), `CompactionNode` (handles `CompactionStart`, `CompactionComplete`, `ContextCompacted`), subagent agent/status/cost/context tracking (from the parent `task` input and child `LLMRequest`, `TurnComplete`, `TurnError`, `TurnInterrupted`), `nodes`/`container_of`/`owner_of` indexes, content-filtered notices (D15), `source_preview`, `tool_detail_sections`. Web: register HTML helpers as Jinja globals in `web.py`; `_nodes.html` gets an iteration macro (`.iteration` with dim left border) and a compaction macro; tool rows stay `<details>` and subagents stay inline for now; `render_change` maps replace targets by node kind. Follow the test style in `tests/test_web_session_detail.py`.
- **Test seam:** `TranscriptBuilder.apply` (new `tests/test_transcript_model.py`); HTTP routes via the existing web tests.
- **Edge cases:** tool result without call → tool in its iteration group; child events before the task tool call → subagent at root (existing fallback); duplicate event ids → no changes; stream reconnect mid-reply → remove then append (existing).
- **Deliverable:** the web viewer shows the tools of one iteration grouped in one dim-bordered block, rendered from the shared model.
- **Verify:** `scripts/dev test tests/test_transcript_model.py tests/test_web_session_detail.py tests/test_web_session_live.py` passes; full `scripts/dev test` has no new failures.
- **Outcome:** Built `shared/src/archie_shared/transcript.py` (builder, nodes, indexes, `IterationNode`, `CompactionNode` including `CompactionStart`, subagent agent/status/cost/activity, content-filtered notices, presentation helpers `tool_headline`/`tool_extra`/`tool_preview`/`tool_detail_sections`). `orchestrator/.../transcript.py` now holds only HTML helpers; `web.py` registers them as Jinja globals and maps replace targets by node type. Web groups tools in `.iteration` blocks with a dim left border. Evidence: `scripts/dev test` 2574 passed (baseline 2552 + 22 new); ruff check/format clean on touched files. Web render of the WAF log: 0.78–0.88 s, 10.2 MB (budget 1.0 s / 13.5 MB). Deviation: `read` results are not highlighted (see Deviations).

### [x] M2 — Web tool and subagent overlays with breadcrumbs (tracer for the popups)

- **Approach:** `_nodes.html`: a tool is `<div class="tool" id>` holding `<div id="{head_id}">` (row `<button class="tool-line" data-dialog>` with summary and the 10-line exec/shell preview, plus `<dialog class="overlay">` with breadcrumb slot, metadata and highlighted sections from `tool_detail_sections`), then the children container. A subagent is a row button (`id=row_id`: agent, index, status, cost) plus a `<dialog>` holding `items_id`. New `static/viewer/overlay.js`: delegated clicks on `[data-dialog]`, `showModal()`, an open stack, breadcrumb text from each level's `data-crumb`, crumb clicks close the levels above, and reopen-after-swap on `htmx:oobAfterSwap` and after `live.js` appends. Load it on every detail page. CSS for rows, dialogs and crumbs. Update the web tests for the new markup.
- **Test seam:** HTTP routes (`tests/test_web_session_detail.py`, `tests/test_web_session_live.py`); browser check with `agent-browser`.
- **Deliverable:** the web viewer opens tool and subagent overlays with highlighted content and breadcrumbs; Esc closes the top one.
- **Verify:** web tests pass. `/tmp/078-webbench.py` on the WAF log stays under 1.0 s and 13.5 MB. Browser check: copy the WAF log to `/tmp/078-home/sessions/`, run `ARCHIE_HOME_DIR=/tmp/078-home scripts/dev run uvicorn archie_orchestrator.app:app --port 8765`, open `http://127.0.0.1:8765/ui/sessions/tillo-platform-01m61zwmar` in `agent-browser`; click a shell tool → dialog shows highlighted input; open a subagent → click one of its tools → breadcrumb shows three levels; Esc closes one level; with a tool dialog open, run `htmx.swap` with a re-rendered copy of its fragment → the dialog is open afterwards. Screenshots saved for the PR.
- **Outcome:** Tool rows are buttons (`.tool-line`) showing the summary, a 10-line exec/shell preview and `… +N lines`; each has a `<dialog>` with breadcrumb, metadata and Pygments-highlighted sections. Subagent rows (`.sub-row`: agent, index, cost, status) open a dialog holding the child transcript; its tool rows open nested dialogs. New `static/viewer/overlay.js` (open stack, crumbs, Escape closes one level, crumb clicks close levels above, reopen after live swaps via a `viewer:fragment` event from `live.js`). A live tool update now replaces only `#tool-…-head`, so nested subagents stay mounted. Evidence: `scripts/dev test` 2577 passed; WAF page render 0.87–0.98 s, 12.3 MB (budget 1.0 s / 13.5 MB). Browser check in Chromium (`agent-browser`, viewer routes served by `/tmp/078-serve.py`, because the full app's lifespan needs Docker): subagent → its Read tool gave breadcrumb `tillo-platform-01m61zwmar › build-it-reviewer #0 › Read …`; Escape closed one level, then the next; root crumb closed all; with both dialogs open, an `htmx.swap` of the tool head left both open and Escape still worked. Screenshots in `/tmp/078-shots/`. Deviation: Escape is handled in script, not by the native dialog cancel (see Deviations).

### [x] M3 — TUI renders from the shared model with cheap rows

- **Approach:** New `cli/src/archie_cli/tui/transcript_view.py` with `TranscriptView` (unwindowed here; `suspend`/`resume` exist), a node→widget factory reusing message widgets from `conversation.py`, an `IterationBlock` container with `ToolRow`/`SubagentRow` `Static`s (D6, D18), the dim border in `archie.tcss` (D7), and 10-line exec/shell previews via `source_preview` (D8). Replace transcript handling in `app.py::_render_canonical`; delete `_handle_scoped_event`, `_iteration_block`, `_pending_tool_inputs`, `_pending_compactions`, `_child_*`, `_parent_task_*`, `_transient_assistant_text`, `_finalized_assistant_requests` and `subagents.py`'s `ChildActivityState`/`SubagentActivity`. The main view appends `TurnStatus` when a root turn ends, using the app's live wall time when known. `add_client_error` and `add_shell_output` append local rows. `SubagentProvider` and `ctrl+x` read `app.transcript.subagents` (the palette still opens the old status screen until M4). Rewrite assertions in `tests/test_tui_canonical.py` and `tests/test_tui_subagents.py` that touch removed internals, keeping each scenario.
- **Test seam:** `ArchieApp._apply_event` against a mounted `Conversation` (`_ConversationHost`, `tests/test_tui_canonical.py:248`).
- **Edge cases:** final assistant text differs from streamed text → stream row replaced; empty user message → no row; content-filtered → error row; interrupted assistant → "Cancelled" row.
- **Deliverable:** the TUI shows the same transcript as before, with dim-bordered iteration blocks and single-widget tool rows.
- **Verify:** `scripts/dev test tests/test_tui_canonical.py tests/test_tui_subagents.py tests/test_tui_models_provider.py tests/test_tui_shell.py` passes; full suite has no new failures.
- **Outcome:** New `cli/src/archie_cli/tui/transcript_view.py` (`TranscriptView`/`Conversation`, `IterationBlock` with `ToolRow`/`SubagentRow` single-`Static` rows, `SubagentBlock`, node→widget factory, change application, `suspend`/`resume`). `ArchieApp._render_canonical` now dedups, updates accounting, applies `self.transcript` (shared builder) and dispatches changes; `_handle_scoped_event`, the child/pending-tool/transient-text state and `ToolEntry` are gone. Replay suspends the view and mounts once. Dim `IterationBlock` border in `archie.tcss`. Tests rewritten against the model and mounted rows (`test_tui_canonical.py`, `test_tui_subagents.py`, plus `test_kiro_translate.py` and `test_orchestrator_bounce.py`, which mocked the old `Conversation` API); new tests for pending/complete row summaries, parallel grouping, the dim border and 10-line exec/shell rows. Evidence: `scripts/dev test` 2578 passed; ruff clean. WAF benchmark (`/tmp/tui_bench_078.py`): replay 7.0 s (was 26.8–28.7 s), 7,451 widgets (was 8,905), keypress 202.5 ms (was 238.7 ms), relayout 284 ms. Screenshot `/tmp/078-shots/tui-m3.png`.

### [x] M4 — TUI popups, subagent transcript, breadcrumbs

- **Approach:** New `cli/src/archie_cli/tui/detail.py`: `DetailScreen(ModalScreen)` with a breadcrumb header (parent crumbs + own) and `Binding("escape", "dismiss")` so the app's interrupt binding is not reached; `ToolDetailScreen(tool_node)` renders `tool_detail_sections` capped by D17 with `rich.syntax.Syntax` (lexer from the language hint, else from the filename); `SubagentScreen(subagent_node)` hosts a `TranscriptView` over the subagent's items under a status line. `ToolRow.on_click`/`SubagentRow.on_click` post messages that the app handles by pushing the next screen with the current crumbs. `app._dispatch_changes` forwards changes to open screens. `ctrl+x` targets per D16. `_reset_replay_state` pops all detail screens. Palette opens the new `SubagentScreen`.
- **Test seam:** pilot clicks on mounted rows in a test app driven through `ArchieApp._apply_event` (new `tests/test_tui_detail.py`).
- **Edge cases:** click on a pending tool → popup shows "running" and fills in on result; nested subagent → crumbs grow; Esc with no popup → existing interrupt behaviour.
- **Deliverable:** clicking tools and subagents opens popups with highlighted content, breadcrumbs and Esc-to-back.
- **Verify:** `scripts/dev test tests/test_tui_detail.py` passes; full suite has no new failures; an SVG screenshot of a popup from `App.save_screenshot` is saved for the PR.
- **Outcome:** New `cli/src/archie_cli/tui/detail.py`: `DetailScreen` (breadcrumb header, screen-local `escape` → dismiss), `ToolDetailScreen` (status line, `tool_detail_sections` rendered with `rich.syntax.Syntax`, capped by D17, live update when the result arrives), `SubagentScreen` (status line plus a `TranscriptView(root_slot=sub.items_id)` built from the node and fed live changes). `ToolRow`/`SubagentRow` post `ToolClicked`/`SubagentClicked`; `ArchieApp` pushes the screens with the current crumbs, forwards every change batch to open popups, derives the `ctrl+x` target from the topmost `SubagentScreen` (D16) and pops popups on a full reset. `subagents.py` removed; the palette opens the new screen. Evidence: `scripts/dev test` 2588 passed (new `tests/test_tui_detail.py`: 10 tests covering click → popup, live result, Escape without interrupt, subagent transcript with live child tool, nested tool popup and crumbs, Escape back one level at a time, `ctrl+x` target, reset, size cap); ruff clean. Screenshots: `/tmp/078-shots/tui-subagent.png`, `tui-subagent-tool.png`, `tui-exec-popup2.png`.

### [x] M5 — TUI windowing

- **Approach:** In `TranscriptView`: a rows list, window `[start, end)`, constants 200/80/40 (D9), `WindowBoundary` rows with hidden counts, `watch_scroll_y` shifts at the top (earlier) and bottom (later), anchor restore via `call_after_refresh` + `scroll_to_widget(anchor, top=True)` (follow `/workspace/_research/tau/src/tau_coding/tui/widgets.py:961` `_shift_window`). Live appends mount only when the window is at the latest, trimming the top beyond overscan. A root `UserMessage` jumps to the latest window. Replay uses `suspend`/`resume`. Applies to the main view and `SubagentScreen`. Write `/tmp/tui_bench_078.py` from the brain entry's script, driving `ArchieApp._apply_event` with `historical=True` around `suspend`/`resume`.
- **Test seam:** `TranscriptView` in a test `App` fed through `ArchieApp._apply_event`.
- **Edge cases:** shift while streaming → the streaming row is rendered from `StreamNode.text` when remounted; updates for unmounted rows are ignored.
- **Deliverable:** long sessions mount a bounded window and still scroll back through all history.
- **Verify:** new windowing tests pass; `scripts/dev run python /tmp/tui_bench_078.py ~/.nexus/sessions/tillo-platform-01m61zwmar.jsonl` meets the Performance AC numbers, recorded here.
- **Outcome:** `TranscriptView` keeps every row in `rows` and mounts only `rows[start:end]` (`WINDOW_ROWS`/`WINDOW_PAGE`/`WINDOW_OVERSCAN` = 200/80/40, overridable per view). `WindowBoundary` rows show the hidden counts above and below. `watch_scroll_y` schedules `shift_window` at the top and bottom edges; the edge row stays mounted and is restored with `scroll_to_widget(..., top=…)` after the refresh. Live appends mount while the window is at the latest, trimming to 200 once 240 are mounted; rows added while the user reads history only increase the lower count, and follow-scrolling is off while the window is shifted. A root turn (`UserMessage`) jumps to the latest window. `resume` mounts only the latest window, one batch. `SubagentScreen` gets the same behaviour through `TranscriptView`. Evidence: new `tests/test_tui_window.py` (6 tests: replay window, page earlier with the anchor at the viewport top and every row reachable, page later with the anchor kept, live trim beyond overscan, rows counted while reading history, return to latest on a new user message); the 64 existing TUI tests pass. WAF benchmark: replay 0.74 s (target < 10 s; M3 7.0 s), 970 widgets (target < 2,500), 188 focusable, 45 `Markdown`, all 1,505 rows kept in the model, relayout 49 ms (M3 284 ms), keypress 70.3 ms (target < 110 ms; empty app 69.7 ms).

### [x] M6 — MarkdownStream streaming

- **Approach:** `StreamingMessage` gets a stream from `Markdown.get_stream()` on mount, seeds it with the node's current text, writes each `append_text` fragment in order, and `finish()` stops the stream and swaps the header; remove the 0.1 s full-text throttle (D10).
- **Test seam:** `StreamingMessage` via `ArchieApp._apply_event` with `TextDelta` events.
- **Deliverable:** streaming renders only new fragments.
- **Verify:** streaming tests pass; `scripts/dev run python /tmp/078-stream-bench.py ~/.nexus/sessions/tillo-platform-01m61zwmar.jsonl` reports a last-10-tick average under 100 ms, recorded here.
- **Outcome:** `StreamingMessage` composes an empty `Markdown` and runs a worker that owns a `Markdown.get_stream()` stream. `append` adds to `text` and wakes the worker, which writes everything after its written offset, so fragments that arrive before mount are not lost and a row remounted by the window starts from the node's current text. `finish` swaps the header and ends the worker, which stops the stream and does one full `update` only if the rendered source differs from the text. The 0.1 s full-text throttle is gone. The `Markdown` starts empty because a `Markdown` built with initial text records its last parsed line past a trailing blank line, and the next `append` then overwrote the last block (a heading turned into the following paragraph). Evidence: new `tests/test_tui_streaming.py` (3 tests: deltas through `ArchieApp._apply_event` reach `MarkdownStream.write` as fragments with `Markdown.update` patched to fail, and the block structure is right; the final `AssistantMessage` keeps the streamed widget; a remounted row renders earlier text and keeps streaming). `scripts/dev test` 2597 passed; `ruff check .` and `ruff format --check .` clean. `/tmp/078-stream-bench.py` (drives `StreamingMessage`, waits per tick until the fragment is rendered): 9,802 chars, 123 ticks, first-10 avg 97 ms, last-10 avg 91 ms (target < 100 ms; baseline 163 ms). The prototype's 68 ms measured `MarkdownStream.write` alone, without waiting for the render. WAF replay after M6: 0.77 s, 970 widgets, keypress 65.4 ms.

## Deviations

- M1: `read` results are shown as plain text, not highlighted by file type. They carry a `File:` header and `N|` line-number prefixes, so a lexer misparses them, and highlighting them cost 0.4 s of the WAF page render (1.3 s total, over the 1.0 s budget). `write` content and `edit` old/new text are still highlighted by file type. Narrows the highlighting requirement for `read` only.

- M2: Escape is handled by `overlay.js` (keydown) instead of only the native `<dialog>` cancel. Chromium did not deliver Escape to a dialog that a live swap had reopened with `showModal()` outside a user gesture. Behaviour is the same; the mechanism differs from D13's "native dialog gives Esc".
- M2: the browser check served the viewer routes from `/tmp/078-serve.py` (a Starlette app built from `web.py`'s handlers) instead of `uvicorn archie_orchestrator.app:app`, because the orchestrator's lifespan calls Docker, which this container lacks.

- M3: an interrupted assistant message with no text renders as a hidden row; the turn's `TurnInterrupted` notice supplies the single "Cancelled" row (previously the TUI could show two). `test_kiro_translate.py` and `test_orchestrator_bounce.py` were also rewritten (they mocked the removed `Conversation` methods).

## Risks

- `tests/test_tui_canonical.py` (1,177 lines) asserts many removed internals; rewriting them risks dropping scenarios. Mitigation: keep each test's scenario and change only the assertion seam.
- Anchor-preserving window shifts may jitter in a real terminal; tests run headless only.
- Native `<dialog>` nested inside another open dialog relies on top-layer stacking; checked in Chromium only.
- `MarkdownStream` timing in headless tests needs explicit pauses; flaky tests possible.

## Review

- Plan review: 1 pass. Fixed: M1 labelled prefactor and milestones reordered so popups land in M2 (web) and M4 (TUI); exact Verify commands and thresholds for streaming (M6) and the browser check (M2, including how a live swap is exercised without a running session); streaming requirement restated as behaviour; added the return-to-latest AC; split the exec AC by milestone; Tau path made absolute; `/tmp/tui_bench_078.py` marked new; `CompactionStart` handling stated; decisions D16 (`ctrl+x` target), D17 (popup cap), D18 (subagent row placement), D19 (iteration creation); web page-size bound. Nothing rejected.
- Plan review (detail):
- Code review:
- Merge risk:

## Outcome

PR: pending
CI: pending

## Follow-ups

- Long single-line shell commands still wrap in full in the row headline (the 10-line cap counts newlines). Consider truncating the headline by width.

- Update the brain entry `projects/archie/nexus-tui-performance.md` with the delivered design and measurements.
