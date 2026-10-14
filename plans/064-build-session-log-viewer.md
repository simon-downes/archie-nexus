# 064 — Session log viewer

Status: planning
Branch: build-it/064-session-log-viewer (base: main @ 02bfe1c)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/064-session-log-viewer
Updated: 2026-10-14

## Objective

There is no way to browse past sessions except by reading raw JSONL under
`~/.nexus/sessions/`. This build adds a read-only browser view to the orchestrator:
`/ui/sessions` lists every persisted session log (newest first) with workspace, models,
usage and an active marker, filtered by one search box; `/ui/sessions/<id>` renders the
transcript, and follows an active session live.

## Context

User request (paraphrased closely):

- `/ui/sessions` shows previous sessions, newest first, in a table: session id, workspace
  name, models used (ordered by turn count), usage (input, cache read/write, output token
  counts and total cost), and an active marker.
- One search input accepting `"quoted phrase" word before:2026-10-11 after:2026-07-11
  workspace:prefix-or-name`.
- A link to `/ui/sessions/<id>` showing the transcript in a structured, coloured view —
  not a JSON blob or a wall of text. Use the TUI as a guide, but don't mimic it.
- Active sessions receive live updates; the view is read-only.
- Stack: nimble.css, plain JavaScript, htmx. Other small focused libraries are fine; no
  React/Vue, no npm build step.
- Turn markdown must stay visually distinct from the interface chrome.
- The orchestrator probably can't run in this container: take care, test thoroughly.

Codebase facts found at intake:

- The orchestrator (`orchestrator/src/archie_orchestrator/`) is Starlette + Jinja2. `web.py`
  renders `/` (running sessions, meta-refresh) and `/ui/auth` from `templates/` with
  `static/style.css`. Tests drive the ASGI app with `httpx.ASGITransport`
  (`tests/test_web_auth_status.py`).
- Session logs are `<ARCHIE_HOME_DIR>/sessions/<session-id>.jsonl`, canonical events from
  `shared/src/archie_shared/events.py`, read with `SessionLog` (`shared/.../session/log.py`).
  `llm_request` is the only accounting source (CONTRIBUTING invariants).
- Session ids are `{workspace}-{10-char ULID timestamp}`; `split_id()` splits them.
- Running sessions come from `docker.list_sessions()` (shells out to `docker ps`).
- Live events: the agent WebSocket `/stream` sends `handshake`, `session_status`, then every
  public event including live-only `text_delta`; `/events?after=<id>` replays persisted
  history. The TUI's reconciliation is in `cli/src/archie_cli/tui/app.py`
  (`_render_canonical`).
- `shared/src/archie_shared/tool_summaries.py` formats tool summaries as Rich markup, meant
  for any presentation surface.
- Local data: 216 logs, 112 MB; the largest is 11 MB / 4,880 events. Decoding all logs takes
  ~0.8 s, one 11 MB log ~0.08 s.
- `markdown-it-py` 4.2 and `pygments` are already in `uv.lock` (via textual/rich).

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1146 passed; ruff check and format clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (unrelated to this idea)

## Decisions

| #   | Decision | Chosen | Rejected | Why | Source |
|-----|----------|--------|----------|-----|--------|
| D1  | How does a browser get live events for an active session? | Orchestrator SSE endpoint that relays the agent `/stream` WebSocket as HTML fragments | Browser WebSocket to `/sessions/{id}/stream`; polling `/events` | The `/stream` route is bidirectional and accepts commands; a server-side relay keeps the viewer read-only and lets the server render fragments with the same templates as the page | user Q1 |
| D2  | What does free text search? | User and assistant message text (all scopes), session ID, workspace | Also tool inputs and results | Tool output dominates log size (112 MB total vs 5.1 MB of message text) and would make most words match most sessions | user Q2 |
| D3  | Where does the code live? | Three new orchestrator modules: `session_index.py`, `session_search.py`, `transcript.py`; routes in `web.py` | New package; code in `shared/` | Only the orchestrator presents these pages; CONTRIBUTING puts presentation outside `shared/`. Accounting still comes from `llm_request` only | self |
| D4  | Where does history come from? | Local `<home_dir()>/sessions/*.jsonl` read with `SessionLog` | Agent `/events` over HTTP | Stopped sessions have no agent; the orchestrator already reads these files (`lifecycle._remove_empty_session_log`) | self |
| D5  | How is the list kept fast? | In-process `SessionIndex` cache keyed by path, invalidated by `(mtime_ns, size)`; parsing runs in `asyncio.to_thread` | Re-parse every request; SQLite index | A cold scan of local data takes ~0.9 s; with the cache only changed (active) logs are re-read. SQLite adds a migration surface for a read-only view | self |
| D6  | How is "models ordered by turn count" computed? | Per `model_key`, count distinct `(scope, subagent_index, turn)` over `llm_request` with `purpose="conversation"`; sort by count desc, then key | Count requests; count only root turns | "Turns" is the user's word; requests over-weight tool-heavy turns. Compaction requests are not turns | self |
| D7  | Usage totals | Sum of every `llm_request` (all scopes, both purposes): input, cache read, cache write, output, cost | Root scope only | Matches `session_accounting()` totals and the CONTRIBUTING invariant; tests assert equality with it | self |
| D8  | Date filter semantics | Dates are UTC days of the session start (ULID timestamp in the ID). `after:D` includes day D; `before:D` excludes day D | Both inclusive; file mtime | Gmail-style semantics; the ID timestamp is the only start time every log has | self |
| D9  | `workspace:` matching | Case-insensitive: workspace equals the value or starts with it | Substring; exact only | User asked for "prefix-or-name"; equality is a special case of prefix, kept explicit for clarity | self |
| D10 | Search combination | All terms AND; words and phrases are case-insensitive substrings; unknown `key:value` is a word; an unclosed quote runs to the end | OR semantics; error on unknown keys | Narrowing search is what one box with filters implies; never erroring keeps typing-as-you-search usable | self |
| D11 | Invalid date in `before:`/`after:` | Ignore that term and show a notice naming it | Return no rows; HTTP 400 | Search fires on every keystroke (`2026-1` is a normal intermediate state) | self |
| D12 | Front-end assets | Vendor `nimble.min.css` 0.24.1 and `htmx.min.js` 2.0.11 under `static/vendor/` with their MIT/0BSD licence texts | CDN links; htmx SSE extension | The orchestrator is a local tool that may run offline; no build step. The SSE extension's reconnect opens a new `EventSource` that loses `Last-Event-ID`, which would duplicate appended fragments | self |
| D13 | Live client | `static/viewer/live.js` (~60 lines): native `EventSource`, applies each fragment with `htmx.swap()`, reconnects with `?after=<lastEventId>` | htmx-ext-sse | Same reason as D12; `htmx.swap` processes `hx-swap-oob` so the server controls placement | self |
| D14 | Markdown rendering | `markdown-it-py` preset `js-default` (raw HTML escaped, `javascript:` links rejected) + Pygments `HtmlFormatter` for fenced code | mistune; client-side marked.js | Both libraries are already in `uv.lock` (via rich/textual); server rendering keeps page and live fragments identical | self |
| D15 | Tool summary markup (Rich syntax from `tool_summaries.py`) | Own ~40-line parser in `transcript.py` for the subset used (`[bold]`, `[dim]`, `[italic]`, `[red]`, hex colours, `[/]`, `\[`/`\]` escapes) mapped to CSS classes | Add `rich` to the orchestrator and use `Text.from_markup` | `rich` leaves `\]` in plain text (observed: `'ls [a\\]'`), and classes keep colours themable for dark mode | self |
| D16 | Live gap between page load and WebSocket attach | Connect the WebSocket first, then re-read the local log and replay events after the page's cursor; drop WebSocket frames whose `id` was already emitted | Fetch `/events?after=` from the agent | The agent appends before broadcasting (`SessionEventBus.emit`), so anything not in the file at read time arrives on the socket; the file is the same data without an extra HTTP hop | self |
| D17 | Live fragment state | Each SSE connection builds a `TranscriptBuilder` by replaying the whole log silently, then emits fragments only for events after the cursor | Stateless per-event rendering | Tool results need the matching `tool_call` input to format their summary; subagent events need their parent task node | self |
| D18 | Transcript structure | Flat list of turn blocks; tools are `<details>` rows inside the turn; subagent events nest inside their launching `task` tool row, grouped by `subagent_index` | One nested tree per iteration | Flat append targets keep live updates simple; nesting children under the task row mirrors CONTRIBUTING's scope rule (`scope` = task `tool_use_id`) | self |
| D19 | Large content | Tool result and input previews capped at 8,000 characters with "… N more characters" | Full content; separate fetch route | The largest local log is 11 MB, mostly tool output; full content would make multi-MB pages. A raw-content route is a follow-up | self |
| D20 | List size | Show at most 200 rows, newest first, with "Showing 200 of N" | Pagination | Search is the navigation tool; 216 local logs fit on one page today | self |
| D21 | Existing pages | Leave `/` and `/ui/auth` styling unchanged; add a "Session logs" link in `base.html` | Restyle all pages with nimble.css | Not requested; `test_web_console.py` pins the existing design tokens | self |
| D22 | Active sessions unavailable (Docker missing or failing) | Show every row as inactive and a notice "Active status unavailable"; the live endpoint ends with a "not running" notice | Return 500 | The orchestrator may run where `docker` is absent (this container); history should still be browsable | self |
| D23 | Session ID in URLs | Accept only `^[A-Za-z0-9][A-Za-z0-9._-]*$` and an existing `<id>.jsonl` directly under the sessions directory; else 404 | Path join without checks | Prevents path traversal through the `{session_id}` path parameter | self |
| D24 | Content-Security-Policy on viewer pages | `default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; connect-src 'self'`; htmx indicator styles disabled via `htmx-config` meta | No CSP | Logs contain model and tool output; CSP blocks script injection even if escaping regresses | self |
| D25 | Does the live relay feed the metrics writer? | No | Enqueue `llm_request` frames like `proxy_stream` | The viewer is read-only; the CLI's relay already records metrics and `requests` dedupes by `(session_id, event_id)` | self |

## Requirements

**List page**

- R1 MUST serve `GET /ui/sessions` as HTML listing every `*.jsonl` log in `<home_dir()>/sessions`, newest session start first.
  - AC: with logs `a-01k0000000`, `b-01m0000000`, `c-01n0000000`, the rows appear in order c, b, a.
- R2 MUST show per row: session ID linking to `/ui/sessions/<id>`, workspace (`split_id`), start time in UTC, models with turn counts ordered per D6, input / cache read / cache write / output tokens and total cost per D7.
  - AC: a log with model `m1` in turns 1–3 and `m2` in turn 4 renders `m1` before `m2` with counts 3 and 1.
  - AC: the row's token and cost figures equal `session_accounting(path)` for that log.
- R3 MUST mark sessions whose ID is in `list_sessions()` as active.
  - AC: with `list_sessions` patched to return session b, only row b has the active marker.
  - AC: when `list_sessions` raises `OSError` or `DockerError`, the page returns 200, no row is active, and "Active status unavailable" appears.
- R4 MUST render at most 200 rows and say "Showing 200 of N" when more match.
  - AC: with 201 logs, 200 rows render and "Showing 200 of 201" appears.
- R5 MUST keep rendering when a log is unreadable (`EventIdConflict`, `LogAppendError`) or contains invalid lines.
  - AC: a log with one invalid line still renders a row with the valid events' usage; a log with conflicting duplicate IDs renders a row marked "unreadable".

**Search**

- R6 MUST accept `q` on `GET /ui/sessions` and filter rows per D2 and D8–D11: quoted phrases, bare words, `before:YYYY-MM-DD`, `after:YYYY-MM-DD`, `workspace:value`.
  - AC (parser): `"quoted phrase" word before:2026-10-11 after:2026-07-11 workspace:arch` parses to phrases `["quoted phrase"]`, words `["word"]`, before `2026-10-11`, after `2026-07-11`, workspace `arch`.
  - AC: `"fix the bug"` matches a session whose user message contains "Fix the bug" and not one containing "fix a bug".
  - AC: a word that appears only in a tool result does not match.
  - AC: `after:2026-07-11` includes a session started 2026-07-11T00:00:01Z; `before:2026-07-11` excludes it.
  - AC: `workspace:arch` matches workspaces `archie` and `arch`, not `myarch`.
  - AC: `before:2026-13-01` filters nothing by date and the page shows a notice naming `before:2026-13-01`.
- R7 MUST update the table as the user types, without a full page reload, and keep the query in the URL.
  - AC: a request with header `HX-Request: true` returns only the `<tbody id="session-rows">` fragment (no `<html>`); the input carries `hx-get="/ui/sessions"`, `hx-trigger` with a delay, and `hx-push-url="true"`.

**Transcript page**

- R8 MUST serve `GET /ui/sessions/{id}` with a header (ID, workspace, start time, models, usage, active state) and the transcript grouped by turn.
  - AC: a log with two user messages renders two `.turn` blocks, each starting with a `.msg-user` block.
  - AC: unknown IDs and IDs failing D23 (`..%2Fsecret`, `a/b`) return 404.
- R9 MUST render user and assistant text as Markdown inside a `.md` container styled differently from interface chrome, with fenced code highlighted by Pygments.
  - AC: an assistant message with a ```` ```python ```` block renders `<div class="md">` containing `class="highlight"`.
  - AC: a user message `<script>alert(1)</script>` renders as escaped text; no `<script>alert` appears in the response.
  - AC: a link `[x](javascript:alert(1))` does not produce an `href="javascript:`.
- R10 MUST render each tool call as one row with its formatted summary (`format_tool_pending`/`format_tool_complete`), status (pending, ok, error), duration, and an expandable body with the input and the result preview (D19).
  - AC: a `shell` call with `is_error=true` renders a `.tool.error` row whose summary contains `ls` in a `.b` span.
  - AC: a 10,000-character result renders 8,000 characters and "… 2000 more characters".
- R11 MUST render compaction (`context_compacted`, `compaction_complete`), `turn_error`, `turn_interrupted`, and per-turn usage (root `llm_request` totals for that turn).
  - AC: each of those events produces a block with class `.compaction`, `.turn-error`, `.turn-interrupted`; a turn footer shows the turn's root cost.
- R12 MUST nest subagent events (non-null `scope` and `subagent_index`) inside the launching `task` tool row, grouped per `subagent_index`.
  - AC: a `task` call with two children renders two `.subagent` groups inside `#tool-<tool_use_id>`; child assistant text is not rendered at the top level.
- R13 MUST send the CSP header in D24 on `/ui/sessions` and `/ui/sessions/{id}`, and pages MUST work under it (no inline script or style).
  - AC: both responses carry the exact header; the templates contain no `<script>` without `src` and no `style=` attribute.

**Live updates**

- R14 MUST serve `GET /ui/sessions/{id}/live?after=<event_id>` as `text/event-stream` for an active session, relaying agent `/stream` events after the cursor as HTML fragments (`event: fragment`), each persisted event's ID as the SSE `id:`.
  - AC (fake agent WebSocket): with the log holding events e1–e3, `after=e2`, and the socket sending `handshake`, `session_status`, e3 again, e4, the stream contains fragments for e3 and e4 once each, and nothing for e1–e2.
  - AC: a `Last-Event-ID` header takes precedence over `after`.
- R15 MUST stream assistant text live: `text_delta` appends to a streaming block, and the matching `assistant_message` replaces it with rendered Markdown.
  - AC: deltas "Hel", "lo" then `assistant_message` "Hello" produce two `hx-swap-oob="beforeend:#stream-…"` fragments followed by one `hx-swap-oob="outerHTML"` fragment with id `stream-…`.
- R16 MUST end the stream with `event: end` and a "Session ended" notice when the agent socket closes, and with a "Session is not running" notice when the session is not active or the agent is unreachable.
  - AC: each case's stream ends with `event: end` and the notice text.
- R17 MUST keep the viewer read-only: the relay never sends a frame to the agent socket.
  - AC: the fake agent socket records zero `send()` calls.
- R18 MUST update the header usage and the live state indicator during live updates.
  - AC: an `llm_request` frame produces an `hx-swap-oob="outerHTML"` fragment for `#session-usage` with the new totals.
- R19 SHOULD send an SSE comment (`: ping`) every 15 seconds while idle, so proxies and the browser keep the connection. It may be skipped if a test cannot exercise timing without sleeping; the interval is a module constant tests can lower.
  - AC: with the interval set to 0.01 s and an idle fake socket, the stream contains `: ping`.

**Docs**

- R20 MUST add the new routes to the route table in `docs/architecture.md`.
  - AC: `rg -n "/ui/sessions" docs/architecture.md` lists all three routes.

Out of scope: sending messages or interrupts from the browser, deleting or editing logs, authentication, a raw tool-output route, restyling `/` and `/ui/auth`, full-text index persistence, pagination beyond the 200-row cap.

## Design

### Overview

Three orchestrator modules feed two Jinja pages and one SSE endpoint. `session_index` turns each log into a cached `SessionSummary`. `session_search` parses the query and filters summaries. `transcript` reduces events into a node tree (`TranscriptBuilder`) that templates render for the full page; for live updates the same builder reports which node changed, and the endpoint renders that node as an htmx out-of-band fragment.

### Stack

| Dependency | New / reused | Why |
|---|---|---|
| Starlette, Jinja2, httpx, websockets | reused (orchestrator deps) | existing app, templates, test client, agent socket |
| `markdown-it-py>=3` | new direct orchestrator dep; already locked 4.2.0 | D14 |
| `pygments>=2.17` | new direct orchestrator dep; already locked 2.20.0 | D14 |
| nimble.css 0.24.1 (MIT) | new vendored static file | user's choice; classless styling |
| htmx 2.0.11 (0BSD) | new vendored static file | user's choice; search swaps and `htmx.swap()` for live fragments |

### Code structure

- `orchestrator/src/archie_orchestrator/session_index.py` (new): `SessionSummary` (frozen dataclass), `summarize_log(path) -> SessionSummary`, `SessionIndex.summaries(sessions_dir) -> list[SessionSummary]`, `session_start(session_id) -> datetime | None` (decodes the 10-char Crockford ULID timestamp), `sessions_dir() -> Path`.
- `orchestrator/src/archie_orchestrator/session_search.py` (new): `Query` dataclass, `parse_query(text) -> Query`, `matches(summary, query) -> bool`.
- `orchestrator/src/archie_orchestrator/transcript.py` (new): `render_markdown(text) -> Markup`, `markup_to_html(rich_markup) -> Markup` (D15), node dataclasses, `TranscriptBuilder.apply(event) -> list[Change]`, `Change(kind: "append" | "replace" | "append_text", node, parent_id, text)`.
- `orchestrator/src/archie_orchestrator/web.py`: add `session_list_page`, `session_detail_page`, `session_live` handlers. Follow `sessions_page` for template rendering and `proxy.py:proxy_stream` for the `websockets.connect` call and error handling.
- `orchestrator/src/archie_orchestrator/app.py`: register `/ui/sessions`, `/ui/sessions/{session_id}`, `/ui/sessions/{session_id}/live`.
- Templates (new): `templates/viewer/base.html`, `viewer/list.html`, `viewer/_rows.html`, `viewer/detail.html`, `viewer/_nodes.html` (Jinja macros, one per node type; used by page and live fragments).
- Static (new): `static/vendor/nimble.min.css`, `static/vendor/htmx.min.js`, `static/vendor/LICENSES.md`, `static/viewer/viewer.css`, `static/viewer/pygments.css` (generated, see Config), `static/viewer/live.js`.
- `templates/base.html`: one "Session logs" link (D21).
- Tests (new): `tests/test_session_search.py`, `tests/test_session_index.py`, `tests/test_web_session_list.py`, `tests/test_web_session_detail.py`, `tests/test_web_session_live.py`. Follow `tests/test_web_auth_status.py` (ASGI client) and `tests/test_orchestrator_ws_proxy.py` (fake `websockets.connect`).

### Data model

`SessionSummary` (in memory only; lifecycle = process, invalidated per file by `(mtime_ns, size)`):

| Field | Type | Req |
|---|---|---|
| `session_id` | `str` (file stem) | required |
| `workspace` | `str` | required |
| `started_at` | `datetime \| None` (UTC; `None` if the ID has no valid timestamp, sorted last by mtime) | required |
| `models` | `tuple[tuple[str, int], ...]` ordered per D6 | required |
| `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens` | `int` | required |
| `cost_usd` | `float` (rounded 6 dp like `session_accounting`) | required |
| `search_text` | `str` (lower-cased, `\n`-joined session ID, workspace, user and assistant contents) | required |
| `error` | `str \| None` (set when the log cannot be read) | optional |

`Query`: `phrases: list[str]`, `words: list[str]`, `before: date | None`, `after: date | None`, `workspace: str | None`, `notices: list[str]`.

### Data flow

- **List:** request → `asyncio.to_thread(index.summaries, dir)` and `asyncio.to_thread(list_sessions)` → `parse_query` → filter → sort → cap 200 → `list.html` (or `_rows.html` when `HX-Request`).
- **Detail:** validate ID (D23) → `SessionLog(path).read()` in a thread → `TranscriptBuilder` over all events → `detail.html` with `data-last-event-id` of the last persisted event and `data-live-url` when active.
- **Live:** `live.js` opens `EventSource(live_url?after=<last id>)` → handler resolves the session (`proxy._resolve_session` in a thread) → `websockets.connect(ws://127.0.0.1:<port>/stream)` → read log, replay into builder silently up to the cursor, emit fragments for later events (D16, D17) → for each socket frame: decode with `decode_event`, skip `handshake`/`session_status`/`error_notice` and already-emitted IDs, `builder.apply` → render each `Change` as a fragment → `event: fragment\nid: <event id>\ndata: <html lines>`. Socket close → "Session ended" + `event: end`. `live.js` swaps fragments into `#transcript` with `beforeend`, so OOB elements land by ID; on `end` it closes; on error it reconnects after 1, 2, 4… up to 30 s with the latest `lastEventId`.
- **Failure paths:** not active / no port / refused connection → "Session is not running" + `end` (R16). Undecodable socket frame → log at warning, skip. Browser disconnect → Starlette cancels the generator; the `async with websockets.connect` closes the agent socket.

### Error handling

| Failure | Response |
|---|---|
| `list_sessions` raises `OSError`/`DockerError` | fail open: no active markers, notice (D22) |
| Log invalid lines | `SessionLog` skips them (existing warning) |
| `EventIdConflict` / `LogAppendError` on a log | list: row with `error`; detail: 200 page showing the error text |
| Invalid ID or missing file | 404 HTML |
| Agent unreachable / closes | SSE notice + `event: end` (R16) |
| Undecodable live frame | skip, `log.warning` |

### Non-functional

- Security: Jinja autoescape (enabled by `Jinja2Templates` for `.html`); Markdown HTML disabled and link validation from `js-default`; markup parser escapes all text; CSP (D24); ID validation (D23); relay never sends (R17). Routes keep the existing no-auth, trusted-local model (docs/architecture.md).
- Performance: cold list ≤ ~1 s on 112 MB (measured 0.94 s decode); warm list re-reads only changed files. Previews capped at 8,000 chars (D19).
- Observability: `log.warning` on unreadable logs and undecodable frames; `log.info` on live connect/disconnect, following `proxy.py`.

### Config / infra

- Env vars: none new (`ARCHIE_HOME_DIR` via `home_dir()`).
- Generated file: `static/viewer/pygments.css`, produced by `uv run python -m archie_orchestrator.transcript --pygments-css > orchestrator/src/archie_orchestrator/static/viewer/pygments.css` (light `friendly`, dark `github-dark` under `prefers-color-scheme: dark`). A test asserts the committed file equals the generator output.
- Secrets, CI, migrations: none.

## Milestones

### [ ] M1 — Session list page (tracer bullet)

- **Approach:** Build `session_index.py` first (one pass over `SessionLog(path).read()` computing D6/D7 and `search_text`). Add `viewer/base.html` loading the vendored assets and the CSP header (a small helper in `web.py` that wraps `templates.TemplateResponse` and sets the header). Vendor assets with `curl` from jsdelivr and record their SHA-256 in `LICENSES.md`. ⚠️ `list_sessions` raises `FileNotFoundError` when `docker` is missing; catch `OSError` and `DockerError` (D22). ⚠️ Patch `archie_orchestrator.web.list_sessions` in tests, matching `test_web_console.py`.
- **Test seam:** `summarize_log()` and `GET /ui/sessions` through `httpx.ASGITransport` with `ARCHIE_HOME_DIR` set to `tmp_path`.
- **Edge cases:**
  - ID without a valid timestamp → `started_at=None`, sorted after dated logs.
  - Empty sessions dir or missing dir → "No session logs" message.
  - Log with conflicting duplicates → row marked "unreadable" (R5).
- **Tasks:**
  - `session_index.py` with `SessionSummary`, `summarize_log`, `SessionIndex`, `session_start`, `sessions_dir`.
  - Vendor nimble.css and htmx; `LICENSES.md`.
  - `viewer/base.html`, `viewer/list.html`, `viewer/_rows.html`, `viewer.css`; route in `app.py`; link in `base.html`.
  - Tests for R1–R5 and the CSP header part of R13.
- **Deliverable:** `/ui/sessions` lists logs with models, usage and active markers.
- **Verify:** `uv run pytest tests/test_session_index.py tests/test_web_session_list.py` passes; `uv run pytest tests/test_web_console.py` still passes.
- **Outcome:**

### [ ] M2 — Search

- **Approach:** Hand-written tokenizer in `session_search.py` (scan for `"`, whitespace, `key:value`); dates with `date.fromisoformat` restricted to `YYYY-MM-DD` by regex. `matches` checks `search_text` substrings and start date per D8. Search input in `list.html` uses htmx attributes from R7 and targets `#session-rows` with `outerHTML`.
- **Test seam:** `parse_query()` / `matches()` and `GET /ui/sessions?q=…`.
- **Edge cases:**
  - Empty or whitespace query → all rows.
  - Unclosed quote → phrase to end of string.
  - `workspace:` with no value → treated as the word `workspace:`.
  - Session with `started_at=None` and a date filter → excluded.
- **Tasks:**
  - `session_search.py`.
  - Wire `q` into `session_list_page`, notices, `HX-Request` partial.
  - Tests for R6 and R7.
- **Deliverable:** typing in the search box filters the table.
- **Verify:** `uv run pytest tests/test_session_search.py tests/test_web_session_list.py` passes.
- **Outcome:**

### [ ] M3 — Transcript page

- **Approach:** `TranscriptBuilder` follows the event handling in `cli/src/archie_cli/tui/app.py:_render_canonical` and `_handle_scoped_event` (open those for ordering rules), but builds nodes: `TurnNode` (user message, items, footer), `TextNode`, `ToolNode` (call, result, children `SubagentNode`s), `NoticeNode` (compaction, error, interrupted). Node IDs: `turn-<event id>`, `msg-<event id>`, `tool-<tool_use_id>`, `sub-<scope>-<index>`, `stream-<turn>-<iteration>-<request_id>` with scope/index when present, sanitised to `[A-Za-z0-9_-]`. `_nodes.html` has one macro per node type. Markdown: `MarkdownIt("js-default").enable("table")` with a `highlight` callback using Pygments (`get_lexer_by_name`, fallback `TextLexer`). Tool summaries: pending → `format_tool_pending`; result → `format_tool_complete(name, input, content, is_error, duration_ms)`; exec input shown with the Python lexer.
- **Test seam:** `GET /ui/sessions/{id}` through the ASGI client with crafted logs written via `SessionLog.append`, and `markup_to_html()` directly.
- **Edge cases:**
  - Events before any `user_message` (e.g. `session_started`) → header only, no empty turn.
  - `tool_result` without a matching `tool_call` → row with raw preview, status from `is_error`.
  - Child events whose scope has no `task` row → a top-level subagent group in the current turn.
  - `assistant_message` with empty content → no block.
  - Markup `\[exit: 1]` → literal `[exit: 1]`.
- **Tasks:**
  - `transcript.py` (renderers, nodes, builder, `--pygments-css` entry point) and `pygments.css`.
  - `viewer/detail.html`, `viewer/_nodes.html`, styles.
  - Route in `app.py`; 404 handling.
  - Tests for R8–R12 and the rest of R13.
- **Deliverable:** `/ui/sessions/<id>` shows a structured transcript.
- **Verify:** `uv run pytest tests/test_web_session_detail.py` passes; rendering the largest local log (`~/.nexus/sessions`, 11 MB) through `TranscriptBuilder` completes and the page size is reported in the Outcome.
- **Outcome:**

### [ ] M4 — Live updates

- **Approach:** `session_live` returns a `StreamingResponse` from an async generator. Reuse `proxy._resolve_session` (in a thread) and `websockets.connect(..., open_timeout=10)` as in `proxy_stream`. Idle pings use `asyncio.wait_for(anext(socket_iter), PING_INTERVAL)`. `TranscriptBuilder.apply` returns `Change`s; `append` with a parent renders `<div hx-swap-oob="beforeend:#<parent>-items">node</div>`, top-level appends go to `#transcript` via the client's `beforeend` swap, `replace` renders the node with `hx-swap-oob="outerHTML"`, `append_text` renders `<span hx-swap-oob="beforeend:#<stream id>">escaped text</span>`. Usage header re-rendered on `llm_request`. Multi-line HTML is sent as multiple `data:` lines. `live.js` is loaded only when the page is active.
- **Test seam:** `GET /ui/sessions/{id}/live` through `httpx.ASGITransport` (the response completes because the fake socket closes), with `archie_orchestrator.proxy.list_sessions` and `archie_orchestrator.web.websockets.connect` patched as in `test_orchestrator_ws_proxy.py`.
- **Wiring:** state = per-request `TranscriptBuilder` + `emitted_ids: set[str]`; producer = agent socket frames and the log re-read; consumer = browser `EventSource` in `live.js`; call site = `detail.html` `data-live-url`.
- **Edge cases:**
  - Cursor not in the log (`CursorNotFound`) → replay everything after the start (emit all), since the page would be stale.
  - Socket sends a persisted event already emitted from the log re-read → skipped (R14).
  - `text_delta` after its `assistant_message` → ignored, as the TUI does with `_finalized_assistant_requests`.
  - Session not active → notice + `end` without connecting.
- **Tasks:**
  - `session_live` handler and route.
  - Fragment rendering helpers in `transcript.py`/`_nodes.html`.
  - `static/viewer/live.js` and the live indicator in `detail.html`.
  - Tests for R14–R19.
  - Route table in `docs/architecture.md` (R20).
- **Deliverable:** an open transcript of an active session updates as the agent works.
- **Verify:** `uv run pytest tests/test_web_session_live.py` passes; full `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` pass; `rg -n "/ui/sessions" docs/architecture.md` shows three routes.
- **Outcome:**

## Change log

## Risks

- The orchestrator cannot start sessions in this container (no Docker), so live updates are tested against a fake agent socket and, for browser evidence, a stand-in agent; a real agent session is not verified.
- htmx `htmx.swap()` processing of `hx-swap-oob` inside SSE-delivered content is confirmed from the 2.0.11 source but must be observed in a browser (agent-browser) during review.
- Local historical logs contain lines the current schema rejects; those events are silently missing from transcripts (existing `SessionLog` behaviour).

## Review

## Outcome

PR: pending

## Follow-ups
