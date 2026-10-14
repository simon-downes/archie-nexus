# 064 — Session log viewer

Status: done
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
| D12 | Front-end assets | Vendor `https://cdn.jsdelivr.net/npm/@leftium/nimble.css@0.24.1/dist/nimble.min.css` (MIT) and `https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js` (0BSD) under `static/vendor/`, with licence texts from each package's `LICENSE` | CDN links; htmx SSE extension | The orchestrator is a local tool that may run offline; no build step. The SSE extension's reconnect opens a new `EventSource` that loses `Last-Event-ID`, which would duplicate appended fragments | self |
| D13 | Live client | `static/viewer/live.js` (~60 lines): native `EventSource`, applies each fragment with `htmx.swap()`, reconnects with `?after=<lastEventId>` | htmx-ext-sse | Same reason as D12; `htmx.swap` processes `hx-swap-oob` so the server controls placement | self |
| D14 | Markdown rendering | `markdown-it-py` preset `js-default` (raw HTML escaped, `javascript:` links rejected, tables on) + Pygments `HtmlFormatter(nowrap=True)` for fenced code; renderer overrides turn table `style="text-align:X"` into `class="align-X"` and images into links (see D24) | mistune; client-side marked.js | Both libraries are already in `uv.lock` (via rich/textual); server rendering keeps page and live fragments identical | self |
| D15 | Tool summary markup (Rich syntax from `tool_summaries.py`) | Own ~40-line parser in `transcript.py`: an open tag is whitespace-separated styles (e.g. `[dim italic]`, `[#676767 italic]`), `[/]` closes the innermost, `\[`/`\]` are literal brackets. Style→class: `bold`→`b`, `dim`→`dim`, `italic`→`i`, `red` and `#ff6d67`→`err`, `#67c26d`→`ok`, `#676767`→`default`; other styles are dropped | Add `rich` to the orchestrator and use `Text.from_markup` | `rich` leaves `\]` in plain text (observed: `'ls [a\\]'`), and classes keep colours themable for dark mode | self |
| D16 | Live gap between page load and WebSocket attach | Connect the WebSocket first, then re-read the local log and replay events after the page's cursor; drop WebSocket frames whose `id` was already emitted | Fetch `/events?after=` from the agent | The agent appends before broadcasting (`SessionEventBus.emit`), so anything not in the file at read time arrives on the socket; the file is the same data without an extra HTTP hop | self |
| D17 | Live fragment state | Each SSE connection builds a `TranscriptBuilder` by replaying the whole log silently, then emits fragments only for events after the cursor | Stateless per-event rendering | Tool results need the matching `tool_call` input to format their summary; subagent events need their parent task node | self |
| D18 | Transcript structure | Flat list of turn blocks; tools are `<details>` rows inside the turn; subagent events nest inside their launching `task` tool row, grouped by `subagent_index` | One nested tree per iteration | Flat append targets keep live updates simple; nesting children under the task row mirrors CONTRIBUTING's scope rule (`scope` = task `tool_use_id`) | self |
| D19 | Large content | Tool result and input previews capped at 8,000 characters with "… N more characters" | Full content; separate fetch route | The largest local log is 11 MB, mostly tool output; full content would make multi-MB pages. A raw-content route is a follow-up | self |
| D20 | List size | Show at most 200 rows, newest first, with "Showing 200 of N" | Pagination | Search is the navigation tool; 216 local logs fit on one page today | self |
| D21 | Existing pages | Leave `/` and `/ui/auth` styling unchanged; add a "Session logs" link in `base.html` | Restyle all pages with nimble.css | Not requested; `test_web_console.py` pins the existing design tokens | self |
| D22 | Active sessions unavailable (`docker` missing: `list_sessions` raises `OSError`) | Show every row as inactive and a notice "Active status unavailable"; the live endpoint ends with a "not running" notice | Return 500 | The orchestrator may run where `docker` is absent (this container); history should still be browsable | self |
| D23 | Session ID in URLs | Accept only `^[A-Za-z0-9][A-Za-z0-9._-]*$` and an existing `<id>.jsonl` directly under the sessions directory; else 404 | Path join without checks | Prevents path traversal through the `{session_id}` path parameter | self |
| D24 | Content-Security-Policy on viewer pages (revised, C2) | `default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`; htmx indicator styles disabled via `htmx-config` meta (`includeIndicatorStyles: false`); Markdown images render as links (`<a href=…>image: alt</a>`) | No CSP; allowing `https:` images; `'unsafe-inline'` styles | Logs contain model and tool output; CSP blocks script injection even if escaping regresses. `default-src` does not cover framing, `<base>` or form targets, so those are set explicitly. Loading remote images from logs on page open would contact arbitrary hosts. Inline styles are avoided by class rewrites (D14) | self |
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
  - AC: when `list_sessions` raises `FileNotFoundError` (no `docker` binary), the page returns 200, no row is active, and "Active status unavailable" appears.
- R4 MUST render at most 200 rows and say "Showing 200 of N" when more match.
  - AC: with 201 logs, 200 rows render and "Showing 200 of 201" appears.
- R5 MUST keep rendering when a log is unreadable (`EventIdConflict`, `LogAppendError`, not valid UTF-8) or contains invalid lines. (revised, C2)
  - AC: a log with one invalid line still renders a row with the valid events' usage; a log with conflicting duplicate IDs, or with bytes that are not UTF-8, renders a row marked "unreadable"; the detail page for either returns 200 with the error.

**Search**

- R6 MUST accept `q` on `GET /ui/sessions` and filter rows per D2 and D8–D11: quoted phrases, bare words, `before:YYYY-MM-DD`, `after:YYYY-MM-DD`, `workspace:value`.
  - AC (parser): `"quoted phrase" word before:2026-10-11 after:2026-07-11 workspace:arch` parses to phrases `["quoted phrase"]`, words `["word"]`, before `2026-10-11`, after `2026-07-11`, workspace `arch`.
  - AC: `"fix the bug"` matches a session whose user message contains "Fix the bug" and not one containing "fix a bug".
  - AC: a word that appears only in a tool result does not match.
  - AC: `after:2026-07-11` includes a session started 2026-07-11T00:00:01Z; `before:2026-07-11` excludes it.
  - AC: `workspace:arch` matches workspaces `archie` and `arch`, not `myarch`.
  - AC: `before:2026-13-01` filters nothing by date and the page shows a notice naming `before:2026-13-01`.
- R7 MUST update the results (table, row count line, and search notices) as the user types, without a full page reload, and keep the query in the URL.
  - AC: a request with header `HX-Request: true` returns only the `<section id="results">` fragment (no `<html>`), and that fragment contains the notices and the count line; a request that also has `HX-History-Restore-Request: true` gets the full page; both responses carry `Vary: HX-Request, HX-History-Restore-Request`; the input carries `hx-get="/ui/sessions"`, `hx-target="#results"`, `hx-swap="outerHTML"`, `hx-trigger` with a delay, and `hx-push-url="true"`.

**Transcript page**

- R8 MUST serve `GET /ui/sessions/{id}` with a header (ID, workspace, start time, models, usage, active state) and the transcript grouped by turn.
  - AC: a log with two user messages renders two `.turn` blocks, each starting with a `.msg-user` block.
  - AC: a valid-pattern ID with no file (`nope-01k0000000`) and IDs failing the D23 pattern (`.hidden`, `bad%20id`) return 404; a `.hidden.jsonl` file placed in the sessions dir is not served.
- R9 MUST render user and assistant text as Markdown inside a `.md` container styled differently from interface chrome, with fenced code highlighted by Pygments.
  - AC: an assistant message with a ```` ```python ```` block renders `<div class="md">` containing `class="highlight"`.
  - AC: a user message `<script>alert(1)</script>` renders as escaped text; no `<script>alert` appears in the response.
  - AC: a link `[x](javascript:alert(1))` does not produce an `href="javascript:`.
  - AC: a right-aligned table column renders `class="align-right"` and the response has no `style=` attribute; `![x](https://e.test/a.png)` renders a link to that URL and no `<img`.
- R10 MUST render each tool call as one row with its formatted summary (`format_tool_pending`/`format_tool_complete`), status (pending, ok, error), duration, and an expandable body with the input and the result preview (D19).
  - AC: a `shell` call with `is_error=true` renders a `.tool.is-error` row whose summary contains `ls` in a `.b` span.
  - AC: a 10,000-character result renders 8,000 characters and "… 2000 more characters".
- R11 MUST render compaction (`context_compacted`, `compaction_complete`), `turn_error`, `turn_interrupted`, and per-turn usage (root `llm_request` totals for that turn).
  - AC: each of those events produces a block with class `.compaction`, `.turn-error`, `.turn-interrupted`; a turn footer shows the turn's root cost.
- R12 MUST nest subagent events (non-null `scope` and `subagent_index`) inside the launching `task` tool row, grouped per `subagent_index`.
  - AC: a `task` call with two children renders two `.subagent` groups inside `#tool-<tool_use_id>`; child assistant text is not rendered at the top level.
- R13 MUST send the CSP header in D24 (revised, C2) on `/ui/sessions` and `/ui/sessions/{id}`, and pages MUST work under it (no inline script or style).
  - AC: both responses carry the exact header; the templates contain no `<script>` without `src` and no `style=` attribute.

**Live updates**

- R14 MUST serve `GET /ui/sessions/{id}/live?after=<event_id>` as `text/event-stream` for an active session, relaying agent `/stream` events after the cursor as HTML fragments (`event: fragment`), each persisted event's ID as the SSE `id:`.
  - AC (fake agent WebSocket): with the log holding events e1–e3, `after=e2`, and the socket sending `handshake`, `session_status`, e3 again, e4, the stream contains fragments for e3 and e4 once each, and nothing for e1–e2.
  - AC: a `Last-Event-ID` header takes precedence over `after`.
  - AC: all fragments caused by one event are one SSE message carrying that event's id; content containing `\r` cannot start a new SSE line, field or message. (C2)
- R15 MUST stream assistant text live: `text_delta` appends to a streaming block, and the matching `assistant_message` replaces it with rendered Markdown.
  - AC: on a fresh builder with no deltas, `assistant_message` yields the rendered text appended to the turn and a `hx-swap-oob="delete"` element for its `stream-…` id.
  - AC: deltas "Hel", "lo" then `assistant_message` "Hello" produce, in order: one fragment appending a `.streaming` element with id `stream-…` containing "Hel" to the turn's items; one `hx-swap-oob="beforeend:#stream-…-text"` fragment containing "lo"; one `hx-swap-oob="outerHTML:#stream-…"` fragment whose content is the rendered Markdown block. (revised, C1)
- R16 MUST end the stream with `event: end` and a "Session ended" notice when the agent socket closes, and with a "Session is not running" notice when the session is not active or the agent is unreachable.
  - AC: each case's stream ends with `event: end` and the notice text, including when `list_sessions` raises `FileNotFoundError`, and when the log cannot be read ("This log could not be read"). (C2)
- R17 MUST keep the viewer read-only: the relay never sends a frame to the agent socket.
  - AC: the fake agent socket records zero `send()` calls.
- R18 MUST update the header usage during live updates, and show the live state in `#live-state`: "connecting" on page load, "live" after the SSE `open`, "ended" after `event: end`, "reconnecting" after an error.
  - AC: an `llm_request` frame produces an `hx-swap-oob="outerHTML:#session-usage"` fragment with the new totals. (revised, C1)
  - AC: the page for an active session renders `<span id="live-state">connecting</span>` and loads `live.js`; an inactive session renders no `#live-state` and no `live.js`. The JavaScript state changes are checked in the browser (Risks).
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
- `orchestrator/src/archie_orchestrator/transcript.py` (new): `render_markdown(text) -> Markup`, `markup_to_html(rich_markup) -> Markup` (D15), node dataclasses, `TranscriptBuilder.apply(event) -> list[Change]`, `Change(kind: "append" | "replace" | "append_text" | "remove", node, parent_id, target_id, text)`.
- `orchestrator/src/archie_orchestrator/web.py`: add `session_list_page`, `session_detail_page`, `session_live` handlers. Follow `sessions_page` for template rendering and `proxy.py:proxy_stream` for the `websockets.connect` call and error handling.
- `orchestrator/src/archie_orchestrator/app.py`: register `/ui/sessions`, `/ui/sessions/{session_id}`, `/ui/sessions/{session_id}/live`.
- Templates (new): `templates/viewer/base.html`, `viewer/list.html`, `viewer/_results.html`, `viewer/detail.html`, `viewer/_nodes.html` (Jinja macros, one per node type; used by page and live fragments).
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

- **List:** request (partial only when `HX-Request` is set and `HX-History-Restore-Request` is not) → `asyncio.to_thread(index.summaries, dir)` and `asyncio.to_thread(list_sessions)` → `parse_query` → filter → sort → cap 200 → `list.html` (or `_results.html`, the `<section id="results">` with notices, count line and table, when `HX-Request`).
- **Detail:** validate ID (D23) → `SessionLog(path).read()` in a thread → `TranscriptBuilder` over all events → `detail.html` with `data-last-event-id` of the last persisted event and `data-live-url` when active.
- **Live:** `live.js` opens `EventSource(live_url?after=<last id>)` → handler resolves the session (`proxy.resolve_session` in a thread) → `websockets.connect(ws://127.0.0.1:<port>/stream)` → read log, replay into builder silently up to the cursor, emit fragments for later events (D16, D17); if the cursor is not in the log, send `event: reload` and stop (`live.js` reloads the page) → for each socket frame: decode with `decode_event`, skip `handshake`/`session_status`/`error_notice` and already-emitted IDs, `builder.apply` → render each `Change` as a fragment → `event: fragment\nid: <event id>\ndata: <html lines>`. Socket close → "Session ended" + `event: end`. `live.js` swaps fragments into `#transcript` with `beforeend`, so OOB elements land by ID; on `end` it closes; on `reload` it calls `location.reload()`; on error it reconnects after 1, 2, 4… up to 30 s with the latest `lastEventId`.
- **Failure paths:** not active / no port / `list_sessions` raising `OSError` / refused connection → "Session is not running" + `end` (R16). Undecodable socket frame → log at warning, skip. Browser disconnect → Starlette cancels the generator; the `async with websockets.connect` closes the agent socket.

### Error handling

| Failure | Response |
|---|---|
| `list_sessions` raises `OSError` (no `docker`) | fail open: no active markers, notice (D22) |
| Log invalid lines | `SessionLog` skips them (existing warning) |
| `EventIdConflict` / `LogAppendError` on a log | list: row with `error`; detail: 200 page showing the error text |
| Invalid ID or missing file | 404 HTML |
| Agent unreachable / closes, or `docker` missing during resolve | SSE notice + `event: end` (R16) |
| Live cursor not in log | `event: reload` |
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

### [x] M1 — Session list page (tracer bullet)

- **Approach:** Build `session_index.py` first (one pass over `SessionLog(path).read()` computing D6/D7 and `search_text`). Add `viewer/base.html` loading the vendored assets and the CSP header (a small helper in `web.py` that wraps `templates.TemplateResponse` and sets the header). Vendor assets with `curl` from jsdelivr and record their SHA-256 in `LICENSES.md`. ⚠️ `list_sessions` raises `FileNotFoundError` when `docker` is missing (it returns `[]` on a non-zero exit); catch `OSError` (D22). ⚠️ Patch `archie_orchestrator.web.list_sessions` in tests, matching `test_web_console.py`.
- **Test seam:** `summarize_log()` and `GET /ui/sessions` through `httpx.ASGITransport` with `ARCHIE_HOME_DIR` set to `tmp_path`.
- **Edge cases:**
  - ID without a valid timestamp → `started_at=None`, sorted after dated logs.
  - Empty sessions dir or missing dir → "No session logs" message.
  - Log with conflicting duplicates → row marked "unreadable" (R5).
- **Tasks:**
  - `session_index.py` with `SessionSummary`, `summarize_log`, `SessionIndex`, `session_start`, `sessions_dir`.
  - Vendor nimble.css and htmx; `LICENSES.md`.
  - `viewer/base.html`, `viewer/list.html`, `viewer/_results.html`, `viewer.css`; route in `app.py`; link in `base.html`.
  - Tests for R1–R5 and the CSP header part of R13.
- **Deliverable:** `/ui/sessions` lists logs with models, usage and active markers.
- **Verify:** `uv run pytest tests/test_session_index.py tests/test_web_session_list.py` passes; `uv run pytest tests/test_web_console.py` still passes.
- **Outcome:** Built `session_index.py` (`SessionSummary`, `summarize_log`, `summarize_events`, `SessionIndex`, `session_start`, `sessions_dir`), vendored assets with `LICENSES.md` (SHA-256 recorded), `viewer/base.html`, `list.html`, `_results.html`, `viewer.css`, `session_list_page` + route, and a "Session logs" link in `base.html`. Tests: a `session_logs` fixture in `tests/conftest.py`; `uv run pytest tests/test_session_index.py tests/test_web_session_list.py tests/test_web_console.py` → 33 passed. Against the 216 local logs: cold request 0.91 s, warm 0.01 s, 200 rows, "Showing 200 of 216"; checked in agent-browser. `uvicorn` startup needs a `docker` binary (existing `lifespan` calls `list_sessions` without catching `FileNotFoundError`); a stub `docker` on PATH was used for the browser check, and this was left unchanged (out of scope).

### [x] M2 — Search

- **Approach:** Hand-written tokenizer in `session_search.py` (scan for `"`, whitespace, `key:value`); dates with `date.fromisoformat` restricted to `YYYY-MM-DD` by regex. `matches` checks `search_text` substrings and start date per D8. Search input in `list.html` uses the htmx attributes from R7.
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
- **Outcome:** Built `session_search.py` (`Query`, `parse_query`, `matches`) and wired `q`, notices, the `#results` partial, history-restore full page, and `Vary` into `session_list_page`. `uv run pytest tests/test_session_search.py tests/test_web_session_list.py` → 32 passed. In agent-browser against local logs: typing `workspace:tillo after:2026-10-01` swapped in 4 rows and pushed `?q=…`; `before:2026-1` showed the notice; focus stayed in the input; no console errors under the CSP.

### [x] M3 — Transcript page

- **Approach:** `TranscriptBuilder` follows the event handling in `cli/src/archie_cli/tui/app.py:_render_canonical` and `_handle_scoped_event` (open those for ordering rules), but builds nodes: `TurnNode` (user message, items, footer), `TextNode`, `ToolNode` (call, result, children `SubagentNode`s), `NoticeNode` (compaction, error, interrupted). Node IDs: `turn-<event id>`, `msg-<event id>`, `tool-<tool_use_id>`, `sub-<scope>-<index>`, `stream-<turn>-<iteration>-<request_id>` with scope/index when present, sanitised to `[A-Za-z0-9_-]`. `_nodes.html` has one macro per node type. Markdown: `MarkdownIt("js-default", {"highlight": fn})`. `fn(code, lang, attrs)` returns `<pre class="highlight"><code>` + `pygments.highlight(code, lexer, HtmlFormatter(nowrap=True))` + `</code></pre>` (markdown-it uses output starting with `<pre` as-is); lexer from `get_lexer_by_name(lang)`, fallback `TextLexer`. Override `renderer.rules["th_open"/"td_open"]` to move `style="text-align:X"` to `class="align-X"`, and `rules["image"]` to render `<a href=src>image: alt</a>` (D24). `pygments.css` uses `HtmlFormatter(style=…).get_style_defs(".highlight")`. Tool summaries: pending → `format_tool_pending`; result → `format_tool_complete(name, input, content, is_error, duration_ms)`; exec input shown with the Python lexer.
- **Test seam:** `GET /ui/sessions/{id}` through the ASGI client with crafted logs written via `SessionLog.append`, and `markup_to_html()` directly.
- **Edge cases:**
  - Events before any `user_message` (e.g. `session_started`) → header only, no empty turn.
  - `tool_result` without a matching `tool_call` → row with raw preview, status from `is_error`.
  - Child events whose scope has no `task` row → a top-level subagent group in the current turn.
  - `assistant_message` with empty content → no block.
  - Markup `\[exit: 1]` → literal `[exit: 1]`.
- **Tasks:**
  - Add `markdown-it-py>=3` and `pygments>=2.17` to `orchestrator/pyproject.toml`; run `uv lock` (expect no version changes).
  - `transcript.py` (renderers, nodes, builder, `--pygments-css` entry point) and `pygments.css`.
  - `viewer/detail.html`, `viewer/_nodes.html`, styles.
  - Route in `app.py`; 404 handling.
  - Tests for R8–R12 and the rest of R13.
- **Deliverable:** `/ui/sessions/<id>` shows a structured transcript.
- **Verify:** `uv run pytest tests/test_web_session_detail.py` passes; a one-off ASGI request for the largest local log in `~/.nexus/sessions` (11 MB) returns 200 within 5 s; record the time and page size in the Outcome.
- **Outcome:** Built `transcript.py` (`render_markdown`, `highlight_code`, `markup_to_html`, node dataclasses, `Change`, `TranscriptBuilder`, `build`, `pygments_css` + `--pygments-css` entry point), `viewer/_nodes.html`, `viewer/detail.html`, `viewer/not_found.html`, generated `static/viewer/pygments.css`, transcript styles, `session_detail_page` + `session_log_path` + route; added `markdown-it-py`/`pygments` to `orchestrator/pyproject.toml` (`uv.lock`: 4 lines, no version changes). `uv run pytest tests/test_web_session_detail.py` → 21 passed; full suite 1210 passed; Ruff clean. Largest local log (`archie-01m32ky6rn`, 11 MB): 200 in 0.4 s, 9.7 MB page. Checked in agent-browser: user/assistant blocks, tool rows, expanded tool bodies, no console errors. Deviations: markdown-it render rules take the renderer as first argument (bound methods); the stream element's own id is `stream-…` and its text target is `stream-…-text`, so the final `outerHTML` replace swaps the whole block. Message and section wrappers use `<div>` because nimble.css styles `<article>`/`<header>` as cards. No real local log has decodable subagent message events (old-schema lines are skipped), so subagent nesting is verified by tests only.

### [x] M4 — Live updates

- **Approach:** `session_live` returns a `StreamingResponse` from an async generator. Reuse `proxy.resolve_session` (in a thread; catch `OSError` as well as its `KeyError`/`ValueError`) and `websockets.connect(..., open_timeout=10)` as in `proxy_stream`. A reader task (cancelled and awaited in the generator's `finally`, as `proxy_stream` does at `proxy.py:211-216`) copies socket frames into an `asyncio.Queue` and puts a sentinel when the socket ends; the generator waits with `asyncio.wait_for(queue.get(), PING_INTERVAL)` and sends `: ping` on timeout. ⚠️ Never `wait_for` the socket iterator itself: the timeout cancels and finishes its async generator, so the next read looks like a closed socket. `TranscriptBuilder.apply` returns `Change`s; `append` with a parent renders `<div hx-swap-oob="beforeend:#<parent>-items">node</div>`, top-level appends go to `#transcript` via the client's `beforeend` swap, `replace` renders the node with `hx-swap-oob="outerHTML"`, `append_text` renders `<span hx-swap-oob="beforeend:#<stream id>">escaped text</span>`. The first `text_delta` for a request key yields an `append` of a `StreamNode` (id `stream-…`, holds the text) into the current turn's items, or the subagent group's items for child events; later deltas yield `append_text`; the matching `assistant_message` always yields `replace` of the deterministic `stream-…` id by its `TextNode`, even when this builder saw no deltas. The fragment is `<div hx-swap-oob="outerHTML:#stream-…">`-wrapped only when that id exists in the builder; otherwise the builder yields an `append` of the `TextNode` plus a `remove` Change for `#stream-…` (rendered `<div id="stream-…" hx-swap-oob="delete"></div>`), which clears a partial block left by an earlier connection. `live.js` also adds class `stale` to `.stream` elements on reconnect so CSS dims them until replaced. Usage header re-rendered on `llm_request`. Multi-line HTML is sent as multiple `data:` lines. `live.js` is loaded only when the page is active.
- **Test seam:** `GET /ui/sessions/{id}/live` through `httpx.ASGITransport` (the response completes because the fake socket closes), with `archie_orchestrator.proxy.list_sessions` and `archie_orchestrator.web.websockets.connect` patched as in `test_orchestrator_ws_proxy.py`.
- **Wiring:** state = per-request `TranscriptBuilder` + `emitted_ids: set[str]`; producer = agent socket frames and the log re-read; consumer = browser `EventSource` in `live.js`; call site = `detail.html` `data-live-url`.
- **Edge cases:**
  - Cursor not in the log (`CursorNotFound`) → `event: reload`; the page reloads and gets a fresh cursor. No `after` → emit everything.
  - Socket sends a persisted event already emitted from the log re-read → skipped (R14).
  - `text_delta` after its `assistant_message` → ignored, as the TUI does with `_finalized_assistant_requests`.
  - Session not active → notice + `end` without connecting.
- **Tasks:**
  - `session_live` handler and route.
  - Fragment rendering helpers in `transcript.py`/`_nodes.html`.
  - `static/viewer/live.js` and the live indicator in `detail.html`.
  - Tests for R14–R19, including the ping AC with a fake socket that stays idle and then sends one event, which must still be relayed after the ping.
  - Route table in `docs/architecture.md` (R20).
- **Deliverable:** an open transcript of an active session updates as the agent works.
- **Verify:** `uv run pytest tests/test_web_session_live.py` passes; full `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` pass; `rg -n "/ui/sessions" docs/architecture.md` shows three routes.
- **Outcome:** Built `session_live` (SSE `StreamingResponse` over `_live_events`), `render_change`, `static/viewer/live.js`, the route, and the architecture route table + viewer paragraph. `uv run pytest tests/test_web_session_live.py` → 18 passed; full `uv run pytest` → 1228 passed; `ruff check` and `ruff format --check` clean; `rg -n "/ui/sessions" docs/architecture.md` → 4 lines (3 routes + paragraph). Browser check (agent-browser) with a stand-in agent WebSocket and a stub `docker`: indicator went connecting → live → ended; deltas streamed into a `.streaming` block that the final message replaced with rendered Markdown and highlighted code; the tool row went pending → ok; header usage updated to the new totals; no duplicate DOM ids; no console errors; the agent received no client messages; closing the agent showed "Session ended.". Deviations (→ C1): fragments are rendered in Python (`render_change` in `web.py`) instead of a Jinja `change` macro; `replace` fragments use an explicit `outerHTML:#<target>` selector; deltas carry no SSE id (only persisted events do); an unknown cursor sends `reload` after connecting rather than before.

## Change log

- C3 (review, browser evidence): tool row status classes renamed `ok`/`error`/`pending` → `is-ok`/`is-error`/`is-pending` because `.ok` is also the green text utility and tinted all nested content; R10 AC updated. No scope change.
- C2 (review): Review fixes changed behaviour and structure — (a) each event's fragments go out as one SSE message with the event id (was one message per fragment, last one carrying the id), so reconnect cursors never split an event; (b) SSE data is split on CR, LF and CRLF; (c) a newly created subagent group is sent once, already holding its first item; (d) the first `text_delta` a builder sees, and an `assistant_message` with no local stream, emit a removal marker (`<template data-remove-id>`, applied by `live.js` before the swap) instead of htmx `hx-swap-oob="delete"`, which logs `htmx:oobErrorNoTarget` when the target is absent; (e) `read_events` in `session_index.py` also treats `UnicodeDecodeError` as unreadable, and the live relay ends with a notice on an unreadable log; (f) the detail page renders in a worker thread; (g) the relay queue is bounded at 1024 frames, matching the agent; (h) exec source previews are capped; (i) the CSP adds `frame-ancestors`, `base-uri`, `form-action`; (j) token/cost totals use a new `UsageTotals` in `shared/src/archie_shared/session/accounting.py`, which `session_accounting` now also uses; `proxy.resolve_session` was renamed `resolve_session`; number formatting is two Jinja filters (`tokens`, `usd`). Evidence: reviewer reproductions and the new regression tests. No scope change.
- C1 (M4): `replace` fragments carry `hx-swap-oob="outerHTML:#<target_id>"` instead of bare `outerHTML` — a finished stream (`stream-…`) is replaced by a message node with a different id, so htmx's default id matching would miss it; R15/R18 ACs and tests assert the explicit selector. Fragments are built by `render_change` in `web.py` (Python wraps the node macros) rather than a separate Jinja macro, so the root-element `hx-swap-oob` injection lives in one place. Live-only `text_delta` fragments have no SSE `id`, so reconnect cursors are always persisted event ids. No scope change.
## Risks

- The orchestrator cannot start sessions in this container (no Docker), so live updates are tested against a fake agent socket and, for browser evidence, a stand-in agent; a real agent session is not verified.
- htmx `htmx.swap()` processing of `hx-swap-oob` inside SSE-delivered content is confirmed from the 2.0.11 source but must be observed in a browser (agent-browser) during review.
- Local historical logs contain lines the current schema rejects; those events are silently missing from transcripts (existing `SessionLog` behaviour).

## Review

- Plan review: pass 1 ❌ (2 ❌, 6 ⚠️, 6 ℹ️). All 14 agreed and fixed: CSP vs table `style=` (class rewrite, D14/D24, R9 AC); idle ping cancelling the socket iterator (queue + reader task, M4); nimble.css scoped package name (D12); redundant `enable("table")`; `DockerError` never raised (R3, D22); D23 AC unreachable through routing (R8 AC); Pygments wrapper (M3); notices/count outside the swapped fragment (R7 `#results`); missing stream-node creation (R15, M4); D15 compound tags and class names; stale-cursor duplicate append (`event: reload`); `OSError` during live resolve (R16); M3 Verify threshold; remote images (D24).
- Plan review: pass 2 ⚠️ (3 ⚠️, 3 ℹ️). All agreed and fixed: htmx history restore receiving the partial (R7: full page on `HX-History-Restore-Request`, `Vary`); `assistant_message` with no local stream node after reconnect (`remove` Change, R15 AC); live-state indicator AC (R18); stale error-table row; dependency task in M3; reader-task cleanup in M4. Stopped after two passes.

- Code review: round 1, roles correctness, conventions, risk (risk ran: untrusted log content rendered to HTML, a path parameter mapped to a file, a new SSE endpoint opening a WebSocket). Agreed and fixed (C2), each with a regression test that failed first:
  - ❌ first child event duplicated in live view (correctness) → fixed (c); `test_first_child_event_is_sent_once`.
  - ❌/⚠️ bare `\r` ends SSE lines and can inject `id:`/`event:` (correctness, risk) → fixed (b); `test_carriage_returns_cannot_inject_sse_fields`; also observed in agent-browser with a CR-laden tool result.
  - ⚠️ non-UTF-8 log returns 500 on list and detail (correctness) → fixed (e); `test_non_utf8_log_does_not_break_the_list`, `test_detail_non_utf8_log`.
  - ⚠️ reconnect mid-stream duplicates the final message (correctness) → fixed (d); `test_resumed_stream_replaces_stale_block`; observed in agent-browser by killing the orchestrator mid-stream: reconnect resumed with the stored cursor, no duplicate ids.
  - ⚠️ detail render blocks the event loop ~0.3 s on an 11 MB log (risk) → fixed (f); `test_detail_render_runs_off_the_event_loop`.
  - ℹ️ unreadable log in the live relay reconnects forever (correctness, risk) → fixed (e); `test_unreadable_log_ends_stream`.
  - ℹ️ loose `"b" in data` selector in a test (correctness) → fixed.
  - ❓ split SSE messages per event (correctness) → answered by fixing: one message per event (a); `test_each_event_is_one_sse_message`.
  - ℹ️ unbounded relay queue (risk) → fixed (g). ℹ️ uncapped exec source (risk) → fixed (h); `test_exec_source_is_capped`.
  - ❓ missing `frame-ancestors`/`base-uri`/`form-action` (risk) → added (i); `test_csp_blocks_framing_and_base`.
  - ℹ️ duplicated accounting loops (conventions) → fixed (j) with shared `UsageTotals`.
  - ℹ️ exec label duplicates `format_tool_activity`; number formatting in two places; `__dict__` rebuild; lazy `session_index` undocumented; cross-module private import; missing docstrings (conventions) → all fixed.
  - ❓ is `--pygments-css` a documented command (conventions) → yes: noted in the generated file header and enforced by `test_pygments_css_file_is_current`, whose failure message names the file; added to CONTRIBUTING (see Outcome).
  - Rejected: none.
- Round 2 (correctness only, the role with round-1 ❌):
  - ❌ a child's first `assistant_message` was still sent twice: that branch created the group through `_container` before `_add` → fixed: `_subagent` records groups created by the current event and `_add` skips the item append into such a group; `test_first_child_assistant_message_is_sent_once` (failed first).
  - ⚠️ reconnect while a child streams left the old, emptied group in the page with a duplicate id → fixed: a new group is preceded by a removal marker for its own id; `test_resumed_child_stream_replaces_stale_group` (failed first).
  - ❓ stale `proxy._resolve_session` references in the plan → updated.
  - Stopped after two rounds (skill limit); the round-2 ❌ is fixed and covered by a test that failed before the fix.
- Browser evidence pass found one more defect (treated as ❌): tool rows used the status as a bare class (`ok`/`error`), which collided with the `.ok`/`.err` colour utilities, so everything inside a successful `task` row (subagent messages) was green. Fixed by renaming the row classes to `is-ok`/`is-error`/`is-pending` (C3); re-checked in agent-browser (computed colour of nested text is the body colour).
- Merge risk: Medium: the worst remaining failure is a live-view glitch (a misplaced or stale fragment until reload) or a slow first list load on a very large sessions directory; the routes are read-only, untrusted log content is escaped and covered by a CSP, and the one shared change (`UsageTotals` behind `session_accounting`) keeps the same output, which the existing accounting tests check.

## Outcome

Delivered a read-only session log viewer in the orchestrator:

- `/ui/sessions` lists every `<ARCHIE_HOME_DIR>/sessions/*.jsonl`, newest first, with workspace, start time, models ordered by turn count, input/cache read/cache write/output tokens, cost, and an active marker. One search box takes words, `"phrases"`, `after:`/`before:` dates and `workspace:` prefixes, updates the table as you type (htmx), and keeps the query in the URL.
- `/ui/sessions/<id>` renders the transcript by turn: user and assistant Markdown in a reading style distinct from the chrome, collapsible tool rows with summaries from `tool_summaries.py`, inputs and capped results, subagent groups nested under their `task` row, compaction/error/interrupt notices, and per-turn usage.
- For an active session, `live.js` follows `/ui/sessions/<id>/live` (SSE), which relays the agent `/stream` read-only as htmx fragments: streamed text, tool progress, subagents, usage and a live-state badge; it resumes from the last persisted event id after a drop.

Verification (all run in the worktree):

- `uv run pytest` → 1240 passed (baseline 1146; 94 new tests); `uv run ruff check .` and `uv run ruff format --check .` clean.
- Real local data (216 logs, 112 MB): list cold 0.91 s, warm 0.01 s; largest transcript (11 MB log) 200 in 0.4 s, 9.7 MB page.
- agent-browser, with a stand-in agent WebSocket and a stub `docker`: search swaps, notices, history; streaming → final Markdown; tool pending → ok; subagent nesting; usage updates; reconnect after killing the orchestrator mid-stream (resumed with the stored cursor, no duplicate ids); "Session ended" on agent close; no console errors under the CSP.

Not verified: a real agent container (Docker is not available here), so the relay was exercised against a stand-in agent that follows the `/stream` contract, not the real agent; dark mode was not looked at; browsers other than headless Chromium.

PR: pending

## Follow-ups
