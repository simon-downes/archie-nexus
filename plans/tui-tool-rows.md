# TUI tool rows: examples to review

Draft for the `archie tui` prototype. Every native tool, in the proposed two-line
layout. Most inputs, results and timings come from real calls in `~/.nexus/sessions`.
Some variants are made up because no call in the logs matched: the image read, the
shell timeout, the exec error, the skill references, the web search with results,
the fetch with search terms, and every shell or exec description. Edit freely; the
prototype will follow this file.

## Layout

```
<glyph> <Tool> · <metrics> · <duration> · <error>
╰ <target>
```

Every row is exactly two lines.

- **Line 1:** glyph, the tool name in bold, then muted metrics and the duration. A failed
  call ends with its error in red.
- **Line 2:** what the call acted on, muted, after a muted `╰` under the glyph, so the
  target lines up with the tool name. It comes from the input, so it's there while the
  call runs and the row doesn't change height when it finishes.

No grouping of consecutive calls for now.

## General rules

- **Glyphs:** spinner while running, green `●` on success, red `✕` on failure, yellow `?`
  while a question waits.
- **Metrics:** outcome counts first, then the duration. At most two counts.
- **Durations:** `6ms` under a second, `1.7s` under a minute, `2m35s` above.
- **Errors:** last on line 1, in red, cut with `…` to fit; the full text is in the tool
  detail view.
  - The exception class prefix is dropped (`FileNotFoundError: File not found: x` →
    `File not found: x`), except for exec, where the class is often the message
    (`KeyError: 'team'`).
  - If the message ends with the target already on line 2, that part is dropped
    (`File not found: /tmp/a.png` → `File not found`).
  - Shell and exec use the last non-empty line of the output, which is usually the
    useful one (pytest summary, traceback tail).
- **`/workspace`:** stripped everywhere on the row, even when the model included it: in
  paths, working directories, commands and error text. `/workspace/archie-nexus/cli`
  shows as `archie-nexus/cli`; `/workspace` on its own isn't shown. The detail view
  keeps the exact input.
- **Other paths:** `~` for the home directory. A path that doesn't fit loses its middle
  (`archie-nexus/…/prototype/data.py`) so the filename stays.
- **Shell working directory:** shown as `· in <dir>` at the end of line 2 when it isn't
  the workspace root. The command or description is cut to make room, so the directory
  is never the part that's lost.
- **Shell and exec descriptions** assume a new optional `description` input (one
  sentence on what the call does). Without one, shell shows the command's first line
  after `$`, and exec shows the source size.

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

### write

```
● Write · 8 lines · 8ms
╰ archie-nexus/cli/src/archie_cli/tui/prototype/__init__.py
```

### edit

```
● Edit · +4 −1 · 7ms
╰ archie-nexus/cli/src/archie_cli/tui/prototype/data.py

● Edit · +12 −12 · 6 replacements · 5ms
╰ archie-nexus/cli/src/archie_cli/tui/prototype/app.py

✕ Edit · 2ms · Text not found in file
╰ core/platform-services/waf/tests/main.tftest.hcl
```

The diff moves to the tool detail view.

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
╰ "class (PendingQuestion|QuestionOption|…)" in archie-nexus/shared/src/archie_shared/events.py

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
╰ archie-nexus/.git/build-it-worktrees/079-question-tool/shared/…/transcript
```

### shell

```
● Shell · 45 lines · 123ms
╰ Show the branch, recent commits and TUI file sizes · in archie-nexus

● Shell · 45 lines · 123ms
╰ $ git status --short | head; git log --oneline -5; git branch --show-cur… · in archie-nexus

● Shell · 3 lines · 41ms
╰ $ cd archie-nexus && git log --oneline -3

⠋ Shell · 12s
╰ Run the prototype tests · in archie-nexus

✕ Shell · exit 1 · 9.2s · 1 failed, 10 passed in 8.91s
╰ Run the prototype tests · in archie-nexus

✕ Shell · timed out after 30s
╰ $ archie serve
```

With a description, line 2 is the description and the command is in the detail
view. Without one, line 2 is the command's first line after `$`. The third example
shows `/workspace` stripped from a command the model wrote as
`cd /workspace/archie-nexus && …`.

### exec

```
● Exec · returned 1 line · 273ms
╰ Count the edits made to app.py

● Exec · returned 1 line · 273ms
╰ python · 9 lines

✕ Exec · 1.1s · KeyError: 'team'
╰ Summarise the Datadog monitors by team
```

### web_fetch

```
● Fetch · 97 lines · 229ms
╰ kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/ingress_class/

● Fetch · 12 matches for "loadBalancerAttributes" · 229ms
╰ kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/ingress/ingress_class/

✕ Fetch · 204ms · Non-text content type: image/jpeg
╰ cdn.paralect.com/files/nx7d…/1791486821317-herdr-agents-sidebar.jpg
```

The URL drops `https://`.

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
⠋ Task · 1 of 2 done · 1m12s
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

## Open questions

- Metrics on line 1 left-aligned (as above) or right-aligned in a column?
