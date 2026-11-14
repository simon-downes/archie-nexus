# TUI tool rows

The two-line tool rows of the multi-session TUI (plan 084, D11 and D12). Brought from the
`prototype/tui-workbench` branch's reviewed draft, with D12's corrections applied: a
running row shows the spinner and no elapsed time, a failed shell row shows the exit
code and the last output line, metrics are left-aligned, and shell and exec rows have
no `description` input.

The row model is `shared/src/archie_shared/tool_rows.py`; `cli/src/archie_cli/tui/transcript_view.py`
`ToolRow` draws it. `tests/test_tool_rows.py` checks every example below. Most inputs,
results and timings come from real calls in `~/.nexus/sessions`; the image read, the
shell timeout, the exec error, the skill references, the web search with results and
the fetch with search terms are made up.

## Layout

```
<glyph> <Tool> · <metrics> · <duration> · <error>
╰ <target>
```

Every row is exactly two lines.

- **Line 1:** glyph, the tool name in bold, then muted metrics and the duration,
  left-aligned after the name. A failed call ends with its error in red.
- **Line 2:** what the call acted on, muted, after a muted `╰` under the glyph, so the
  target lines up with the tool name. It comes from the input, so it's there while the
  call runs and the row doesn't change height when it finishes.

No grouping of consecutive calls for now.

## General rules

- **Glyphs:** the animated spinner while running, green `●` on success, red `✕` on
  failure, yellow `?` while a question waits.
- **Running rows** show no elapsed time: the transcript model has no start time for a
  call. Line 1 is the glyph and the name, plus any progress the tool reports (a task's
  finished subagents, a question waiting).
- **Metrics:** outcome counts first, then the duration. At most two counts.
- **Durations:** `6ms` under a second, `1.7s` under a minute, `2m35s` above.
- **Counts** come from the result event's fields where it has one (`result_lines`,
  `duration_ms`, the status) and from the input where it says (a write's content, an
  exec's source); the result text is read only for what no field carries (match and
  file counts, a shell's exit code).
- **Errors:** last on line 1, in red, cut with `…` to fit; the full text is in the tool
  detail view.
  - The exception class prefix is dropped (`FileNotFoundError: File not found: x` →
    `File not found: x`), except for exec, where the class is often the message
    (`KeyError: 'team'`).
  - If the message ends with the target already on line 2, that part is dropped
    (`File not found: /tmp/a.png` → `File not found`).
  - A shell command that exits non-zero shows `exit N` and the last non-empty line of
    its output, which is usually the useful one (pytest summary, traceback tail), and no
    line count.
  - A call interrupted before it finished (the result `cancelled`) shows `cancelled`
    and no duration.
- **`/workspace`:** stripped everywhere on the row, even when the model included it: in
  paths, working directories, commands and error text. `/workspace/archie-nexus/cli`
  shows as `archie-nexus/cli`; `/workspace` on its own isn't shown. The detail view
  keeps the exact input.
- **Other paths:** `~` for the session container's home directory. A path that doesn't
  fit loses its middle (`archie-nexus/…/prototype/data.py`) so the filename stays.
- **Shell working directory:** shown as `· in <dir>` at the end of line 2 when it isn't
  the workspace root. The command is cut to make room, so the directory is never the
  part that's lost.
- **Shell and exec targets:** there is no `description` input. Shell shows the command's
  first line after `$`; exec shows `python · N lines` (the source's size).

## Examples

### read

```
● Read · 80 lines · 6ms
╰ ~/.nexus/brain/projects/archie/nexus-roadmap/tui-sidebar.md

● Read · 141 of 880 lines · 4ms
╰ archie-nexus/shared/src/archie_shared/transcript.py:120-260

● Read · image 1700×1150 · 9ms
╰ /tmp/proto/act.png

✕ Read · 6ms · File not found
╰ /tmp/proto/02-blocked.png
```

An image's size is the original's, before any resizing for the model.

### write

```
● Write · 8 lines · 8ms
╰ archie-nexus/cli/src/archie_cli/tui/prototype/__init__.py
```

### edit

```
● Edit · +4 −1 · 7ms
╰ archie-nexus/cli/src/archie_cli/tui/prototype/data.py

✕ Edit · 2ms · Text not found in file
╰ core/platform-services/waf/tests/main.tftest.hcl
```

`+N` is green and `−N` red. The diff moves to the tool detail view. The edit result
doesn't say how many places a `replace_all` edit changed, so there's no count.

### glob

```
● Glob · 1 file · 26ms
╰ agent/src/archie_agent/**/shell*.py in archie-nexus

● Glob · no files · 12ms
╰ **/*.tcss in archie-nexus/orchestrator
```

### grep

```
● Grep · 3 matches in 1 file · 14ms
╰ "class (PendingQuestion|QuestionOption)" in archie-nexus/shared/src/archie_shared/events.py

● Grep · 23 matches in 6 files · 31ms
╰ "render_tool_row" in archie-nexus/cli/src · *.py

✕ Grep · 9ms · Search path does not exist
╰ "def default_index|def connect" in archie-nexus/orchestrator/src/archie_orchestrator/metrics
```

### code

```
● Code · 22 files · 16ms
╰ archie-nexus/cli

● Code · 4 symbols · 9ms
╰ "SessionList" in archie-nexus/cli

✕ Code · 1ms · Path not found
╰ archie-nexus/…/079-question-tool/shared/src/archie_shared/transcript
```

### shell

```
● Shell · 45 lines · 123ms
╰ $ git status --short | head; git log --oneline -5; git branch --show-cur… · in archie-nexus

● Shell · 3 lines · 41ms
╰ $ cd archie-nexus && git log --oneline -3

⠋ Shell
╰ $ scripts/dev test tests/test_tui_prototype.py · in archie-nexus

✕ Shell · exit 1 · 9.2s · 1 failed, 10 passed in 8.91s
╰ $ scripts/dev test tests/test_tui_prototype.py · in archie-nexus

✕ Shell · timed out after 30s
╰ $ archie serve
```

Line 2 is the command's first line after `$`; the whole command is in the detail
view. The second example shows `/workspace` stripped from a command the model wrote
as `cd /workspace/archie-nexus && …`.

### exec

```
● Exec · returned 1 line · 273ms
╰ python · 9 lines

✕ Exec · 1.1s · KeyError: 'team'
╰ python · 14 lines
```

### web_fetch

```
● Fetch · 97 lines · 229ms
╰ kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/ingress_class/

● Fetch · 41 lines · 229ms
╰ kubernetes-sigs.github.io/…/guide/ingress/ingress_class/ · "loadBalancerAttributes"

✕ Fetch · 204ms · Non-text content type: image/jpeg
╰ cdn.paralect.com/…/1791486821317-herdr-agents-sidebar.jpg
```

The URL drops `https://` and is cut like a path, keeping the host and the last parts.
Search terms go at the end of line 2 and are kept when the URL is cut; the fetch result
doesn't count its matches, so line 1 counts lines.

### web_search

```
● Web search · 8 results · 1.4s
╰ "textual command palette providers"

● Web search · no results · 2.7s
╰ "kiro cli changelog ACP tool_call rawInput MCP deferred tools"
```

### brain_search

```
● Brain search · 10 results · 108ms
╰ "tui sidebar"
```

### skill

```
● Skill · 316 lines · 23ms
╰ notion

● Skill · 2 references · 41ms
╰ build-it · references/plan-template.md, references/review.md

● Skill · already loaded · 2ms
╰ notion
```

### question

```
? Question · waiting for you
╰ What kind of demonstration would you prefer?

● Question · → A coding choice · 15.0s
╰ What kind of demonstration would you prefer?

● Question · dismissed · 15.0s
╰ What kind of demonstration would you prefer?
```

The answer goes on line 1 in place of metrics, so question rows are two lines too.

### task

```
⠋ Task · 1 of 2 done
╰ 2 × archie-worker
    ● archie-worker #0        61.1k     $1.3368 - Completed
    ⠋ archie-worker #1        56.8k     $1.2602 - Reading codex-rs/tui/src/exec_cell/render.rs

● Task · 4 agents · 2m35s
╰ 4 × archie-worker
    ● archie-worker #0 …

✕ Task · cancelled
╰ 2 × default
```

Subagent rows under the task stay as they are today.

### Other tools (plugins, MCP)

```
● notion_search · 1,204 chars · 80ms
╰ query="roadmap", limit=10
```

The fallback: the tool name, the result size and duration, and the first few scalar
inputs as `key=value`.

## Resolved

- Metrics on line 1 are left-aligned after the tool name, not in a right-aligned
  column (D12): a column leaves a wide gap on most rows and moves when the pane resizes.
