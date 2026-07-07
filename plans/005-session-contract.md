# 005 — Cross-Project Session Contract

## Objective

Establish a single, shared definition of "session" spanning the host (CLI), the
container (agent), and the future web client. Today a session is three drifting,
mostly-untyped representations — an ID encoded in a container name, a `docker ps`
dict, and a hand-written JSONL schema. This plan collapses them into one
`archie_shared.session` contract (identity, persistence schema, runtime
descriptor), rebuilds the CLI and agent on top of it, adds the missing
`stop`/`logs` lifecycle commands, and fixes the session-path / fail-fast issues.

## Context

`archie_shared` is already a dependency of **both** `archie_cli` and
`archie_agent` (and will be for the web client), so it is the natural home for a
session contract. Current reality (verified):

- **Identity** — `cli.py:58 generate_session_id()` = `{project}-{ulid[:10].lower()}`
  (project from `detect_project_dir().name`). The ID is embedded in the container
  name `archie-{session_id}` (`cli.py:21,230`) and parsed back by TWO independent
  definitions that must agree: `SESSION_PATTERN` regex (`cli.py:28`, `_CROCKFORD
  {10}`) + `name.removeprefix("archie-")` (`cli.py:157`). Nothing keeps the
  generator and parser in sync.
- **Runtime state** — `list_sessions()` (`cli.py:137-167`) shells `docker ps
  --filter name=archie-` and returns an **untyped dict** per session:
  `{name, session_id, status(raw docker string), port}`. `shell`/`attach` resolve
  by prefix match (`cli.py:326,375`). **`stop` does not exist** (`-d --rm`).
- **Conversation state** — `agent/session.py` `Session`/`Turn`/`TurnLog`,
  container-only. `flush_turn` (`session.py:167-207`) hand-writes a JSONL line.
- **Persisted record** — the JSONL file. VISION.md (`:280-304`) calls it the
  *canonical record* clients replay for catch-up and the host reads for memory
  extraction — but the CLI reads **zero** JSONL today; history comes only from the
  live `/history` endpoint, so a stopped session is unreadable. VISION's schema
  also lists `tools[]` and `metadata.backend`, which the code does **not** emit —
  the two have drifted.
- **Wire** — only `SessionInfo` (`events.py:24-48`, `{protocol_version, model,
  session_id}`) is a formalized shared type. `/status` (`app.py:91-103`) and
  `/history` (`app.py:106-148`) are hand-built untyped dicts.

**Serialization direction:** `shared` uses no msgspec today (pyyaml + frozen
dataclasses). Plan 003 introduces msgspec into `shared`; the new session types
will be msgspec Structs to match.

**Dependency (must land after 003):** `session.py:20` imports
`ModelInfo`/`calculate_cost`, both reshaped by 003 (`ModelEntry`,
`calculate_cost(cost, ...)`); `Session.model_info` becomes `model`. 003 also
delivers `ARCHIE_HOME_DIR` + `config.home_dir()` and the whole-dir host↔container
bind mount at `/home/${USERNAME}/.nexus`. Writing session JSONL to the mounted
sessions dir (so the host can read it directly) depends on both the home-dir
mount and the sessions-path work here.

## Requirements

### Identity (Layer 1)
- MUST provide a single definition of the session-id format and the container-name
  mapping, replacing `cli.py:58` generation + `cli.py:28` regex + `cli.py:157`
  removeprefix + `cli.py:21` prefix.
- MUST NOT change the ID format (`{project}-{ulid[:10].lower()}`) or the
  container-name scheme (`archie-{id}`) — pure relocation + de-duplication.
- MUST expose generate, container-name build, and parse-from-container-name such
  that generator and parser can never drift.

### Persistence schema (Layer 2)
- MUST define one canonical `SessionLogEntry` msgspec Struct = one JSONL line,
  written by the agent.
- MUST design the v1 schema fresh, reconciling code (`session.py:187`) and VISION
  (`:284`): add `metadata.backend`; declare `tools[]` but leave it empty in v1
  (text-only until tool-calling lands).
- MUST tolerate old logs missing `backend` (optional field) so a future reader
  can decode them.
- NOTE: v1 is WRITE-ONLY — no CLI reads JSONL. The files sit on the host mount
  (`home_dir()/sessions/*.jsonl`) for an editor or a model to consume directly.
  No `read_log`/`summarize`/`logs` command in v1 (dropped — no consumer; add a
  reader when the host memory-extraction path actually needs it).

### Runtime descriptor (Layer 3)
- MUST replace the untyped `docker ps` dict returned by `list_sessions()` with a
  typed `SessionDescriptor`. Listing is RUNNING-only (live containers); there is
  no stopped-session status, so no `SessionStatus` enum in v1.
- MUST formalize the `/status` and `/history` payloads as shared types (like
  `SessionInfo`), not ad-hoc dicts.

### Lifecycle & integration
- MUST resolve the agent's session dir at `home_dir()/sessions`; MUST drop
  `ARCHIE_SESSIONS_DIR` (only READ at `app.py:63`, never SET — grep-confirmed).
- MUST fail fast on a missing/empty `ARCHIE_SESSION_ID` (no `"unknown"` fallback).
- MUST add `archie stop` (docker stop; container is destroyed by `--rm`, the JSONL
  log survives on the host mount).
- MUST NOT change the wire event vocabulary or the ID format/scheme.

## Technical Design

### Dependencies
- `msgspec` — added to `shared/pyproject.toml` by 003; confirm present.
- No new third-party deps in cli or agent.

### New package: `shared/src/archie_shared/session/`
Mirrors the `credentials/` package shape from 004. Modules:
`__init__.py` (exports), `identity.py`, `log.py`, `descriptor.py`.

### Identity (`session/identity.py`)
- `CONTAINER_PREFIX = "archie-"` (relocated from `cli.py:21`).
- `generate_session_id(project: str) -> str` — `{project}-{ulid[:10].lower()}`.
  This is a SIGNATURE CHANGE, not a pure port: `cli.py:58-67` takes no args and
  calls `detect_project_dir().name` internally. `detect_project_dir()` walks
  `~/dev` and is HOST-ONLY (the agent/web client have no such concept), so it
  MUST stay in `cli.py`; the shared function takes `project` and the CLI wrapper
  passes `detect_project_dir().name`. ULID timestamp-prefix scheme preserved for
  chronological sortability.
- `container_name(session_id: str) -> str` — `f"{CONTAINER_PREFIX}{session_id}"`.
- `parse_container_name(name: str) -> str | None` — the `SESSION_PATTERN` regex
  (`^archie-(.+)-([0-9A-HJKMNP-TV-Z]{10})$`, IGNORECASE) as the ONE source of
  truth; returns the session_id or `None`.
- `split_id(session_id) -> tuple[str, str]` — `(project, ulid_prefix)` for
  descriptor/display.

### Persistence (`session/log.py`)
- `ToolCall(msgspec.Struct)`: `name`, `source`, `result` (shape from VISION `:291`).
- `EntryMetadata(msgspec.Struct)`: `model`, `backend`, `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost`,
  `interrupted`. `forbid_unknown_fields=True`; `backend`/optional fields default
  so old logs load. `cost` MUST preserve the current 6-dp rounding
  (`session.py:198` `round(cost, 6)`) — apply in `write_entry`/`flush_turn`, not
  silently dropped.
- `SessionLogEntry(msgspec.Struct)`: `id`, `when` (ISO-8601 UTC), `user`,
  `assistant: str | None = None`, `tools: list[ToolCall] = []`,
  `metadata: EntryMetadata`.
- `write_entry(path, entry)` — append one JSONL line; lazy `mkdir(parents=True,
  exist_ok=True)` (retains current agent behaviour). This is the ONLY log function
  in v1 (agent writes; nothing reads).

### Runtime descriptor (`session/descriptor.py`)
- `SessionDescriptor(msgspec.Struct)` — typed `docker ps` row for a RUNNING
  session: `session_id`, `container_name`, `port: int | None`,
  `raw_docker_status: str` (the raw `docker ps` Status string, for display).
  `project` derivable via `split_id` if display wants it. No `status` field / no
  enum — everything `list_sessions()` returns is running by definition.
- `StatusPayload(msgspec.Struct)` — typed `/status` (from `app.py:95-102`):
  `status: str` (`"ok"`/`"starting"`), `model: str`, `session_id: str`,
  `turn_count: int` (`session.turn_index`), `turn_active: bool`
  (`agent.turn_active`). The 503 `{"status": "starting"}` early-return
  (`app.py:94`) is a subset — the other fields are Optional / omitted when starting.
- `HistoryTurn(msgspec.Struct)` — typed `/history` element (from `app.py:140-146`):
  `turn_index: int`, `role: str` (`"user"`/`"assistant"`),
  `content: list[ContentBlock]`. REUSE the existing `ContentBlock` union
  (`types.py:54` = `TextBlock | ToolUseBlock | ToolResultBlock`) rather than
  redefining block structs; the hand-built dicts at `app.py:118-139` are exactly
  those blocks serialized, so this replaces the manual `match` with the union's
  own encoding.

### What gets removed / changed
- `cli.py`: `CONTAINER_PREFIX` (`:21`), `_CROCKFORD` (`:27`), `SESSION_PATTERN`
  (`:28`), `generate_session_id` (`:58`), `removeprefix` (`:157`) → calls into
  `session/identity.py`. `list_sessions()` returns `list[SessionDescriptor]`.
- `app.py`: `ARCHIE_SESSIONS_DIR` read (`:63`) removed; `"unknown"` fallback
  (`:62`) removed; `/status` + `/history` return typed payloads.
- `session.py`: `flush_turn` builds a `SessionLogEntry` via `session/log.py`
  instead of a hand-built dict.

### What stays unchanged
- The ID format and container-name scheme.
- The wire event vocabulary (`TextDelta`/`UsageUpdated`/`TurnComplete`/etc.).
- `SessionInfo` stays the WS-connect event; `StatusPayload` is the separate HTTP
  `/status` type. Recommend keeping both (different lifecycles), not unifying.

## Milestones

### 1. Identity module (`session/identity.py`)

**Approach:**
- Create `shared/src/archie_shared/session/{__init__.py, identity.py}`.
- Port `generate_session_id` **with a `project: str` param** (host injects
  `detect_project_dir().name`; `detect_project_dir` stays in `cli.py`), add
  `container_name`, `parse_container_name`, `split_id`; regex + prefix defined
  once here.

**Edge cases:**
- Non-archie container name → `parse_container_name` returns `None`.
- Project name containing hyphens → regex `(.+)-([crockford]{10})$` anchors on the
  trailing 10-char ULID (greedy project); preserved from current `cli.py:28`.

**Tasks:**
- Implement the four functions + `CONTAINER_PREFIX`.
- Write `shared/tests/test_session_identity.py`: generate format, round-trip
  generate→container_name→parse, reject non-archie, hyphenated project split.

**Deliverable:** one authoritative identity module; generator and parser cannot drift.

**Verify:** `uv run pytest shared/tests/test_session_identity.py -v` — all pass.

---

### 2. Persistence schema + writer (`session/log.py`)

**Approach:**
- Define `ToolCall`, `EntryMetadata`, `SessionLogEntry` structs.
- Implement `write_entry` (append-only). No reader/summary in v1.

**Edge cases:**
- Old log entry missing `backend` → must decode via optional default (schema
  concern, tested by encoding then decoding a `backend`-less line with msgspec).
- `assistant` null → omitted on write (match current behaviour, `session.py:203`).
- `cost` rounded to 6 dp on write.

**Tasks:**
- Implement structs + `write_entry`.
- Write `shared/tests/test_session_log.py`: write a line then msgspec-decode it
  (round-trip proves schema), assistant-omission, missing-`backend` decodes.

**Deliverable:** canonical JSONL schema + append writer.

**Verify:** `uv run pytest shared/tests/test_session_log.py -v` — all pass.

---

### 3. Runtime descriptor + typed payloads (`session/descriptor.py`)

**Approach:**
- Define `SessionDescriptor`, `StatusPayload`, `HistoryTurn` (no `SessionStatus`
  enum — listing is running-only).
- Export the whole session surface from `session/__init__.py` and re-export from
  `shared/src/archie_shared/__init__.py`.

**Edge cases:**
- `port` unpublished / not yet mapped → `port=None`.
- `/status` 503 starting-state → `StatusPayload` non-`status` fields Optional.

**Tasks:**
- Implement the structs; wire exports.
- Write `shared/tests/test_session_descriptor.py`: descriptor construction from a
  `docker ps` row, `StatusPayload`/`HistoryTurn` serialization round-trip
  (incl. `ContentBlock` union encoding).

**Deliverable:** typed running-session listing + HTTP payload contracts.

**Verify:** `uv run pytest shared/tests/test_session_descriptor.py -v` — all pass.

---

### 4. Agent — session path, fail-fast, log writer, typed payloads

**Approach:**
- `app.py`: `session_id = os.environ.get("ARCHIE_SESSION_ID")` → if empty, log a
  single clear line (names the var + that the host injects it) and `sys.exit(1)`
  (no traceback). `sessions_dir = home_dir()/"sessions"`; add
  `home_dir` import; remove `ARCHIE_SESSIONS_DIR`.
- `session.py`: `flush_turn` builds a `SessionLogEntry` (supply `backend`) and
  calls `write_entry(self.log_path, entry)` (path still via the existing
  `log_path` property, which derives from the injected `_log_dir`, `session.py:91-98`);
  update module docstring (`:8`) to `<ARCHIE_HOME_DIR>/sessions/{id}.jsonl`.
- `/status` returns `StatusPayload`; `/history` returns `list[HistoryTurn]`.

**Edge cases:**
- Missing `ARCHIE_SESSION_ID` → fail fast at startup, no partial serving.
- `_log_dir` still injected by app; only its source changes.

**Tasks:**
- Edit `app.py` (env, import, payloads) and `session.py` (flush, docstring).
- Update `agent` tests touching Session/flush/`/status`
  (`test_agent_loop.py`, `test_ws_integration.py`).

**Deliverable:** agent derives its session dir from the home dir, refuses to start
without a session id, and writes the canonical schema.

**Verify:** `uv run pytest` (agent suite) — all pass; manual `docker run` without
`ARCHIE_SESSION_ID` exits non-zero with a clear message.

---

### 5. CLI — identity, typed listing, `stop`

**Approach:**
- Replace `cli.py` identity code (`:21,27,28,58,157`) with `session/identity.py`
  calls. `generate_session_id(project=detect_project_dir().name)` — the
  host-only `detect_project_dir` stays in `cli.py`.
- `list_sessions()` returns `list[SessionDescriptor]`, built from `docker ps`
  (running containers only — no stopped-session scanning). `ls_cmd` display:
  project (via `split_id`), port, raw docker status.
- New `stop` command: resolve session (existing prefix-match/picker logic), run
  `docker stop <container_name>`, report the JSONL log is retained on the host
  mount.

**Edge cases:**
- `stop` on an already-stopped/absent session → clear message, non-zero exit.
- Prefix matches multiple → existing picker (`_pick_session`, `cli.py:399`).
- The `docker logs` hint strings (`cli.py:123,133`) stay as-is (crash-debug
  hints, unrelated to session JSONL).

**Tasks:**
- Refactor identity usage; convert `list_sessions` to `list[SessionDescriptor]`;
  add the `stop` Click command.
- Update the three consumers (`ls_cmd:291`, `shell:315`, `attach:364`) + picker
  to read descriptor fields (`session_id`, `container_name`, `port`).
- Update CLI tests (session listing, id round-trip).

**Deliverable:** CLI speaks the shared contract; listing is typed and
running-only; `stop` tears down a container while preserving its log.

**Verify:** `uv run pytest` (cli suite); manual `archie ls`/`stop` against a
running session.

---

### 6. Docs reconciliation (VISION.md)

**Approach:**
- Update the Session Persistence JSONL schema (`:280-304`) to the exact v1 shape
  (add `backend`, keep `tools[]`, note text-only v1).
- `/archie/sessions` → `<ARCHIE_HOME_DIR>/sessions` (`:51,282,345`).
- Confirm `stop` (`:110`). Note JSONL logs are read directly off the host mount
  (editor/model), not via a CLI command.

**Tasks:**
- Edit VISION.md sections above.

**Deliverable:** VISION matches the implemented contract.

**Verify:** `grep -n "/archie/sessions" VISION.md` returns nothing; schema block
matches `SessionLogEntry`.

## Risks / Open Questions
- **Fail-fast style — DECIDED:** on missing/empty `ARCHIE_SESSION_ID`, log one
  clear line (naming the var + that the host injects it) and `sys.exit(1)` — no
  bare `RuntimeError`/traceback in the container log.
- **`SessionInfo` overlap:** `StatusPayload` (HTTP) overlaps the WS `SessionInfo`.
  Recommend keeping both (different lifecycles), not unifying.
- **Descriptor for web client:** `SessionDescriptor` lives in shared/ anticipating
  a web client even though only the CLI consumes it in v1. Kept deliberately
  minimal (`session_id`, `container_name`, `port`, `raw_docker_status`) —
  project is derivable via `split_id`, so no redundant fields.
