# 013 — TUI Input and Display Improvements

## Objective

Add three quality-of-life TUI improvements: input history cycling (Up/Down arrows),
Ctrl+G to open $EDITOR for composing messages, and git branch display in the status
bar — making the chat interface more productive for terminal power users.

## Context

The current `MessageInput` (`cli/src/archie_cli/tui/input.py`) handles Enter (send) and
Shift+Enter (newline) but has no history or editor support. The `StatusBar`
(`cli/src/archie_cli/tui/status.py`) shows model, tokens, cost, and session ID but no
git branch. The agent runs in Docker with `/workspace` mounted — git branch info should
be part of the wire protocol (resolved on agent side from `/workspace/.git/HEAD`) rather
than requiring client-side git access.

Wire protocol: `shared/src/archie_shared/events.py` defines `SessionInfo` (sent on
connect with `protocol_version`, `model`, `session_id`). Adding `git_branch` here plus
a `StatusUpdated` event for post-turn refreshes is the natural extension.

## Requirements

### Input History

- MUST support Up arrow to show previous sent message when cursor is at start position
  - AC: Empty input + Up → shows most recent sent message
  - AC: Cursor at row 0, col 0 + Up → shows previous message
- MUST support Down arrow to show next message when cursor is at end position
  - AC: At newest history entry + Down → restores draft text
- MUST preserve unsubmitted text when cycling
  - AC: Type "hello", Up (previous), Down → input returns to "hello"
- MUST store history in-memory only (no persistence across sessions)
- MUST NOT interfere with normal Up/Down cursor movement in multi-line input
  - AC: Up on line 2 of a 3-line input moves cursor up (does not cycle history)

### External Editor

- MUST bind Ctrl+G to open `$EDITOR` (default: `nano`) for message composition
  - AC: Ctrl+G suspends TUI, opens editor with current input text
- MUST auto-submit on save (non-empty content)
  - AC: Saving with content immediately sends the message
- MUST clear input on save with empty content
- MUST be a no-op when editor exits without saving (mtime unchanged)
- MUST skip when turn is active
- SHOULD use `.md` suffix for tempfile

### Git Branch Display

- MUST display git branch in status bar
  - AC: On a branch shows name; detached HEAD shows first 8 chars; no git shows "—"
- MUST deliver git branch via wire protocol
  - AC: `SessionInfo` includes `git_branch` field
- MUST refresh branch after each turn completes
  - AC: New `StatusUpdated` event carries `git_branch` after turn ends
- MUST read `/workspace/.git/HEAD` directly (no subprocess)
- MUST maintain backward compatibility
  - AC: `SessionInfo.from_json()` handles missing `git_branch` (defaults to "—")

## Technical Design

### 1. Input History (`MessageInput`)

Add `_history: list[str]`, `_history_idx: int`, `_draft: str` to `MessageInput`.

On submit: append to history, reset index to `len(history)`, clear draft.

In `_on_key`:
- `up`: if cursor at (0, 0) or text empty → save draft (if at end), decrement index,
  load history entry. Prevent default.
- `down`: if cursor at last row, last col → increment index. If past end, load draft;
  else load entry. Prevent default.
- Otherwise: let TextArea handle normally.

### 2. Ctrl+G Editor (`ArchieApp`)

Add `Binding("ctrl+g", "editor", "Editor", show=False)` to `BINDINGS`.

`action_editor()`:
1. Guard: return if `_turn_active`
2. Get current text from MessageInput
3. Write to tempfile (`.md` suffix)
4. Record mtime
5. `with self.suspend(): subprocess.run([editor, tmpfile])`
6. If mtime unchanged → no-op
7. If content non-empty → auto-submit via `MessageInput.Submitted`
8. If content empty → clear input
9. `finally`: delete tempfile

Default editor: `os.environ.get("EDITOR", "nano")`

### 3. Wire Protocol: git_branch

Extend `SessionInfo`:
- Add `git_branch: str = "—"`
- `to_json()`: include in data
- `from_json()`: `data.get("git_branch", "—")` (backward-compatible)

New `StatusUpdated` event:
```python
@dataclass(frozen=True)
class StatusUpdated:
    git_branch: str
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, data: dict) -> StatusUpdated: ...
```

No `turn_index` (session-level event, like `SessionInfo`).

Agent-side helper `_read_git_branch()`:
- Read `/workspace/.git/HEAD`
- Parse `ref: refs/heads/<branch>` → branch name
- Otherwise → first 8 chars (detached HEAD)
- Missing/unreadable → "—"

### 4. StatusBar Extension

Add `git_branch: reactive[str] = reactive("—")` to `StatusBar`.
Render before model name: `⎇ {branch} │ {model} │ ...`

### 5. Event Dispatch

- `SessionInfo` handler: set `status.git_branch = event.git_branch`
- `StatusUpdated` handler: set `status.git_branch = event.git_branch`
- Harness broadcasts `StatusUpdated` after each turn ends

## Milestones

### 1. Input History Cycling

**Approach:**
Extend `MessageInput` with history state. Intercept Up/Down only at boundary positions
(first line for Up, last line for Down).

**Tasks:**
- Add `_history`, `_history_idx`, `_draft` to `MessageInput.__init__`
- Modify `_on_key`: add Up handler (at-start guard), Down handler (at-end guard)
- On Submitted: append to history, reset index
- Create `tests/test_tui_input.py`:
  - Up cycles through history
  - Down returns to draft
  - Multi-line input: Up/Down on middle lines is normal cursor movement
  - Empty history: Up/Down are no-ops
  - Draft preserved across cycling

**Deliverable:** Up/Down cycle through history at input boundaries; draft preserved.

**Verify:** `uv run pytest tests/test_tui_input.py -v` passes.

**Edge Cases:**
- Empty history → no-op
- Single entry → Up shows it, Down restores draft
- Rapid cycling → index clamped to valid range

---

### 2. Ctrl+G External Editor

**Approach:**
Add Textual action binding. Use `self.suspend()` context manager to pause TUI,
run editor as blocking subprocess.

**Tasks:**
- Add `Binding("ctrl+g", "editor", "Editor", show=False)` to `ArchieApp.BINDINGS`
- Implement `action_editor()` with guard, tempfile, suspend, mtime check, submit/clear
- Handle `FileNotFoundError` from editor not found → notify user

**Edge Cases:**
- `$EDITOR` not set → defaults to `nano`
- Editor not found → catch `FileNotFoundError`, notify
- Turn active → skip

**Deliverable:** Ctrl+G suspends TUI, opens editor, auto-submits on save.

**Verify:** `uv run ruff check cli/` clean. Manual test for editor integration.

---

### 3. Git Branch in Wire Protocol

**Approach:**
Extend `SessionInfo` with `git_branch`. Add `StatusUpdated` event. Add
`_read_git_branch()` helper to agent. Agent reads branch and includes in
`SessionInfo` on connect.

**Tasks:**
- Extend `SessionInfo` in `events.py`: add field, update to/from_json
- Add `StatusUpdated` dataclass to `events.py`
- Register `StatusUpdated` in `_SERVER_EVENT_TYPES` and type union
- Handle in `deserialize_event()` (no turn_index, like SessionInfo)
- Add `_read_git_branch()` to `agent/src/archie_agent/app.py`
- Pass `git_branch=_read_git_branch()` to `SessionInfo` in `stream()`
- Add tests: branch ref, detached HEAD, missing file, backward compat

**Edge Cases:**
- `.git/HEAD` trailing newline → `.strip()`
- Empty file → "—"
- Permission denied → "—"
- Symlinked .git (worktrees) → follows symlinks

**Deliverable:** `SessionInfo` carries git branch; `StatusUpdated` event exists.

**Verify:** `uv run pytest tests/ -v -k "git_branch or status_updated"` passes.

---

### 4. StatusBar Display + Harness Broadcast + TUI Wiring

**Approach:**
Add `git_branch` reactive to StatusBar. Harness broadcasts `StatusUpdated` after each
turn ends. TUI handles both events.

**Tasks:**
- Add `git_branch` reactive to `StatusBar`, render in display
- In `_handle_event()`: handle `SessionInfo` → set git_branch; handle `StatusUpdated`
  → set git_branch
- In harness `handle_message()`: broadcast `StatusUpdated(git_branch=_read_git_branch())`
  after turn ends (in finally block after setting `_turn_active = False`)
- Import `_read_git_branch` in harness (or move to utility module)
- Update WS integration test to verify `StatusUpdated` after turn

**Deliverable:** Git branch in status bar, refreshed after each turn.

**Verify:** `uv run ruff check && uv run pytest -q` — all pass.
