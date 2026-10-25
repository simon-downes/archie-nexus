# 073 — Jira Markdown descriptions and comments

Status: planning
Branch: build-it/073-jira-markdown-adf (base: main @ b051ac1)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/073-jira-markdown-adf

## Objective

The Jira plugin sends a description or comment as one ADF paragraph with one text node. The
Jira editor therefore shows multiline text as one line, and nothing can be formatted. Reads
flatten ADF by joining text with spaces, so the agent sees every issue as a wall of text, and
a read-edit-save destroys existing formatting. When done, writes accept Markdown and send
structured ADF, and reads return Markdown, so formatting survives a round trip.

## Context

- User request: handle Jira's formatting features so descriptions are easy to read rather
  than walls of text. Markdown input is accepted ("certainly an improvement"). Expand
  sections render inline on read; @mentions are not needed on write and show as the user's
  name on read.
- Bug record: brain entry `projects/archie/jira-adf-newline-rendering-bug.md` (JS-988,
  JS-989, JS-990 render as one line). Root cause: `_adf()` in `plugins/jira/issues.py`.
- Write paths: `create_issue` and `update_issue` (`description`), `add_comment` (`body`), all
  in `plugins/jira/issues.py`. Read paths: `_detail()` (description) and
  `formatting.comment()` (comment body), plus `add_comment`'s returned body, all through
  `formatting.adf_text`.
- `markdown-it-py` 4.2.0 is already in `uv.lock` (used by `orchestrator/.../transcript.py`).
  Plugins run in the container venv built from the `archie-agent` package
  (`Dockerfile`: `uv sync --package archie-agent`), so the dependency must be declared in
  `agent/pyproject.toml`, the same place `httpx` comes from.
- Jira Cloud rejects descriptions and comments over 32,767 characters ("The entered text is
  too long", Atlassian JRACLOUD-59124). Whether that counts visible text or ADF JSON is not
  known.
- Size rule from the user: if any plan in `plans/` exceeds 8 KB, set the limit to 1.5x the
  largest. A subagent measured 68 plans, 64 over 8,000 characters, largest 71,651
  (`plans/done/028-native-subagent-tool.md`), giving 107,477. That exceeds Jira's cap, so
  the user chose 32,767 (Q1).

## Baseline

- Check commands: `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR UV_PROJECT_ENVIRONMENT=/tmp/nexus-073-venv uv run pytest -q -p no:cacheprovider`; `uv run ruff check .`; `uv run ruff format --check .`
- Setup: `uv sync` into `/tmp/nexus-073-venv`
- Setup leftovers: none
- Failing before this work: none (2,126 passed, 1 warning)
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md` (untracked in the user's checkout)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Write input format | Markdown (CommonMark + GFM tables and strikethrough) | Raw ADF; Jira wiki markup | The model writes Markdown by default; v3 API does not accept wiki markup; raw ADF is verbose and Jira 400s on invalid shapes | user |
| D2 | Expand sections on read | Bold title, then contents inline | Hidden; `<details>` | User request | user |
| D3 | Mentions | Read: `attrs.text` (with `@`), else `@user`; write: `@name` stays text | Resolve `@name` to account IDs | User: mentions not needed | user |
| D4 | Text size limit for description and comment | 32,767 characters | 107,477 (1.5x largest plan); 8,000 | Jira's own cap; larger input would only fail at Jira | user Q1 |
| D5 | Parser | `markdown-it-py` token stream, `commonmark` preset with `html` off, `table` and `strikethrough` enabled | Hand-written parser; `mistune` | Already locked and used in the repo; raw HTML stays literal text instead of being dropped | self |
| D6 | Single newline inside a paragraph | `hardBreak` | Space (CommonMark default) | The bug is lost line breaks; plain multiline text must keep its lines | self |
| D7 | Panels | GitHub alerts: `> [!NOTE]` info, `[!TIP]` success, `[!IMPORTANT]` note, `[!WARNING]` warning, `[!CAUTION]` error; reverse on read | Custom syntax; no panels | Common Markdown convention with a 1:1 mapping to the five panel types | self |
| D8 | Markdown that ADF can't nest | Degrade to allowed nodes, using the allowed-children table in Design: heading → bold paragraph; disallowed quote/panel → its children spliced in; table → one paragraph per row, cells joined by ` \| `; code block in a panel → paragraph of code-marked lines; rule where not allowed → dropped; list item starting with a list → empty paragraph first | Reject input | Jira returns 400 on invalid ADF; degrading keeps the content and never blocks a write | self |
| D9 | Images `![alt](url)` | Link mark with the alt text (or the URL) | `media` nodes | Media nodes need Jira media IDs, not URLs | self |
| D10 | Code where JIRA forbids combined marks | `code` keeps only a `link` mark | Keep all marks | ADF allows `code` only with `link`; other combos are rejected | self |
| D11 | Read rendering of nodes without Markdown form | status → `[text]`; date → `YYYY-MM-DD`; emoji → its text or `:short:`; inline/block/embed card → `<url>`; media → `[attachment: name]`; task item → `- [ ]`/`- [x]`; decision item → `- `; unknown → its text content | Drop them | Content is never silently lost on read | self |
| D12 | Read escaping | Escape only characters that would change the parse: `\`, `*`, `` ` ``, `[`, `\|`, `~` next to `~`, `_` unless between two alphanumerics, `<` that starts an autolink, `&` that starts an entity, and block markers at line start (`#`, `-`/`+` list, `1.`/`1)`, `>`, `=`/`-` underline lines); line-leading whitespace is stripped; emphasis uses `*` | Escape all punctuation; no escaping | Keeps snake_case and prose readable while Markdown → ADF → Markdown → ADF is stable | self |
| D15 | Raw newlines inside stored text nodes (what the old plugin wrote to JS-988–990) | Read as line breaks | Read as spaces | Lets the existing issues be read back with their lines, then re-saved as structured ADF | self |
| D13 | Code location | New `plugins/jira/adf.py` with `markdown_to_adf()` and `adf_to_markdown()`; `formatting.adf_text` replaced by `adf_to_markdown` | Grow `formatting.py` | Converter is ~400 lines and self-contained; testable without the client | self |
| D14 | Missing description on read | `null` | `""` | The skill's documented schema already says "or null" | self |

## Requirements

- MUST convert description and comment Markdown to ADF with headings (levels 1–6),
  paragraphs, hard line breaks, bold, italic, strikethrough, inline code, links, bullet and
  ordered lists (nested, with start number), fenced and indented code blocks (with language),
  blockquotes, rules, GFM tables (header row → `tableHeader`) and alert panels.
  - AC: `test_jira_adf.py` asserts the exact ADF for each construct.
  - AC: `create_issue`, `update_issue` and `add_comment` send that ADF (asserted payload).
- MUST keep every line of plain multiline text as a separate line in ADF.
  - AC: `"a\nb\n\nc"` → paragraph [a, hardBreak, b], paragraph [c].
- MUST produce only ADF that respects node nesting and mark rules: no empty text nodes,
  `code` combined only with `link`, list items starting with a paragraph, no heading/table/
  quote/panel inside list items, quotes or panels other than as D8 allows.
  - AC: a validator in the tests walks every produced document against an allowed-children
    table and passes for the whole test corpus, including the D8 degradation cases.
- MUST keep raw HTML in input as literal text.
  - AC: `"<b>x</b>"` → one text node `"<b>x</b>"`.
- MUST accept description and comment text up to 32,767 characters and reject longer input
  before any request.
  - AC: 32,767 accepted; 32,768 raises `JiraInputError` with no client call.
- MUST return descriptions and comment bodies as Markdown, covering every node the writer
  produces plus D2, D3 and D11.
  - AC: per-node tests on `adf_to_markdown`.
  - AC: a text node containing `"a\nb"` reads as two lines (D15).
- MUST be round-trip stable: for Markdown written by the writer's constructs,
  `markdown_to_adf(adf_to_markdown(markdown_to_adf(m))) == markdown_to_adf(m)`.
  - AC: parametrised round-trip test over the corpus, including text with `*`, `_`, `#`,
    `` ` ``, `[x]`, `1.` and `|`.
- MUST document the Markdown contract, panel syntax, limits and read format in the Jira skill
  and README.
  - AC: `test_jira_skill.py` asserts the skill mentions Markdown and 32,767.

Out of scope: @mention resolution on write; expand, status, date and colour on write; task
lists on write; media upload; summary limit (stays 8,000); repairing JS-988–990 (needs
separate approval); live Jira verification (needs an authorised test issue).

## Design

### Overview

`adf.py` turns `markdown-it-py` block tokens into an ADF tree with a small stack builder,
then normalises nesting per D8. Inline tokens become text nodes with a mark set. The reverse
walker renders blocks recursively with a prefix for list and quote indentation, and renders
inline runs with an open-mark stack so nested marks reopen correctly.

Allowed block children (from Atlassian's ADF node docs), used by the writer's normaliser and
the test validator:

| Container | Allowed |
|---|---|
| doc | paragraph, heading, bulletList, orderedList, codeBlock, blockquote, panel, rule, table |
| listItem | paragraph, bulletList, orderedList, codeBlock (first child paragraph or codeBlock) |
| blockquote | paragraph, bulletList, orderedList, codeBlock |
| panel | paragraph, heading, bulletList, orderedList |
| tableCell / tableHeader | paragraph, heading, bulletList, orderedList, codeBlock, blockquote, panel, rule |

Inline: paragraph and heading hold text and hardBreak; codeBlock holds unmarked text; text is
non-empty; `code` combines only with `link`.

### Stack

- `markdown-it-py>=3` (reused; already in `uv.lock`, newly declared in `agent/pyproject.toml`).

### Code structure

- `plugins/jira/adf.py` (new): `markdown_to_adf(text: str) -> dict`,
  `adf_to_markdown(node: Any) -> str`.
- `plugins/jira/issues.py`: remove `_adf`; use `markdown_to_adf`; `_MAX_BODY = 32767` for
  description and comment; summary keeps `_MAX_TEXT`.
- `plugins/jira/formatting.py`: drop `adf_text`; `comment()` uses `adf_to_markdown`.
- `plugins/jira/tests/test_jira_adf.py` (new); update `test_jira_formatting.py`.

### Error handling

- Malformed ADF on read (non-dict nodes, missing content) renders what text exists and never
  raises.
- No new error types; length errors stay `JiraInputError`.

## Milestones

### [ ] M1 — Markdown descriptions and comments are written as structured ADF

- **Approach:** add the dependency and `markdown_to_adf` per D1, D5–D10. Wire it into the
  three write paths and raise their limit to 32,767 (D4). Update the skill's write sections
  and the README.
- **Test seam:** `markdown_to_adf()`; `create_issue` / `update_issue` / `add_comment` with a
  patched `jira_client` (pattern: `tests/test_jira_review_fixes.py`).
- **Edge cases:** empty string → update clears (unchanged); CRLF → same as LF; trailing
  newlines ignored; empty list item → empty paragraph; empty table cell → empty paragraph;
  Unicode preserved.
- **Deliverable:** a multiline Markdown description reaches Jira as headings, lists and
  separate paragraphs.
- **Verify:** `uv run pytest -q plugins/jira` passes; full checks pass.
- **Outcome:**

### [ ] M2 — Reads return Markdown and round trips are stable

- **Approach:** `adf_to_markdown` per D2, D3, D11, D12, D14; replace `adf_text` in
  `_detail`, `formatting.comment` and `add_comment`. Update the skill's output schema.
- **Test seam:** `adf_to_markdown()`; `get_issue` via `_detail`; round-trip property over the
  corpus.
- **Deliverable:** `get_issue` returns a formatted Markdown description that saves back
  unchanged.
- **Verify:** `uv run pytest -q plugins/jira` passes; full checks pass.
- **Outcome:**

## Deviations

## Risks

- Jira may count ADF JSON size against 32,767, so large formatted text might still 400.
  Needs a live test on an authorised issue.
- Jira's ADF validator may reject a shape the test validator allows; the allowed-children
  table is built from Atlassian's node docs, not from live calls.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Re-save JS-988, JS-989 and JS-990 once rendering is verified (needs approval).
- Live rendering check on a test issue (needs approval).
