# 073 — Jira Markdown descriptions and comments

Status: done
Branch: build-it/073-jira-markdown-adf (base: main @ b051ac1)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/073-jira-markdown-adf

## Objective

The Jira plugin sends a description or comment as one ADF paragraph with one text node. The
Jira editor therefore shows multiline text as one line, and nothing can be formatted. Reads
flatten ADF by joining text with spaces, so the agent sees every issue as a wall of text, and
a read-edit-save destroys existing formatting. When done, writes accept Markdown and send
structured ADF, and reads return Markdown. Formatting the plugin wrote survives a round trip.
Jira-only content (expands, media, mentions, statuses, task lists, colours) reads as readable
text and becomes plain text if re-saved; the skill tells the agent so.

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
| D12 | Read escaping | In inline text only, escape characters that would change the parse: `\`, `*`, `` ` ``, `[`, `\|`, `~` next to `~`, `_` unless between two alphanumerics, `<` that starts an autolink, `&` that starts an entity, and block markers at line start (`#`, `-`/`+` list, `1.`/`1)`, `>`, `=`/`-` underline lines); line-leading whitespace of inline text is stripped. Code spans and code blocks are emitted verbatim. Emphasis uses `*` | Escape all punctuation; no escaping | Keeps snake_case and prose readable while Markdown → ADF → Markdown → ADF is stable | self |
| D13 | Code location | New `plugins/jira/adf.py` with `markdown_to_adf()` and `adf_to_markdown()`; `formatting.adf_text` replaced by `adf_to_markdown` | Grow `formatting.py` | Converter is ~400 lines and self-contained; testable without the client | self |
| D14 | Missing description on read | `null` | `""` | The skill's documented schema already says "or null" | self |
| D15 | Raw newlines inside stored text nodes (what the old plugin wrote to JS-988–990) | Read as line breaks | Read as spaces | Lets the existing issues be read back with their lines, then re-saved as structured ADF | self |
| D16 | Whitespace-only input | Description: treated as empty (create omits it, update clears it). Comment: rejected with `JiraInputError` before any request | Send an empty doc | An empty ADF doc may be rejected by Jira; a blank comment has no meaning | self |
| D17 | Reading tables Jira's editor wrote (multi-block cells, no header row) | Cells: blocks joined with `<br>`, lists flattened to `- item` lines joined with `<br>`, `\|` escaped; no header row: an empty header row is emitted; a row of `tableHeader` cells anywhere is treated as data | Flatten cells to spaces; drop the table | GFM tables can't hold blocks; `<br>` keeps the lines visible, and the writer reads `<br>` literally, so a re-save keeps the text | self |
| D18 | Read marks without Markdown form (`underline`, `textColor`, `backgroundColor`, `subsup`) | Dropped; text kept | HTML tags | Raw HTML stays literal on write (D5), so tags would come back as visible text | self |
| D19 | Converter failure | `markdown_to_adf` never raises on any `str`; the normaliser always places or degrades a node | A new error type | D8's degradations cover every disallowed nesting, so there is nothing left to reject | self |

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
- MUST treat whitespace-only descriptions as empty and reject whitespace-only comments (D16).
  - AC: `create_issue(description="  \n")` sends no description; `update_issue(description=" ")`
    sends `null`; `add_comment(body=" ")` raises `JiraInputError` with no client call.
- MUST NOT raise from `markdown_to_adf` for any string input (D19).
  - AC: a test feeds deeply nested and mixed constructs (quote in list in quote, table in list,
    heading in panel, code in panel, 50-level nesting) and validates the output.
- MUST document the Markdown contract, panel syntax, limits, Markdown read format and the
  Jira-only-content warning in the Jira skill and README.
  - AC: `test_jira_skill.py` asserts the skill mentions Markdown, 32,767 and the warning, and no
    longer says "plain text, not ADF".
  - AC: the README's "How it works" lists `adf.py`.

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

### [x] M1 — Multiline text reaches Jira as separate lines (tracer)

- **Approach:** declare `markdown-it-py>=3` in `agent/pyproject.toml` and run `uv lock`. Create
  `adf.py` with `markdown_to_adf` handling paragraphs, `hardBreak` (D6), plain text, raw HTML
  as text (D5) and CRLF. Wire it into `create_issue`, `update_issue` and `add_comment`
  (removing `_adf`), raise their description/comment limit to 32,767 (D4), and apply D16.
- **Test seam:** `markdown_to_adf()`; the three write functions with a patched `jira_client`
  (pattern: `plugins/jira/tests/test_jira_review_fixes.py`).
- **Edge cases:** `""` on update → clears (unchanged); CRLF → same as LF; trailing newlines
  ignored; Unicode preserved; 32,768 characters → `JiraInputError` with no client call.
- **Deliverable:** the JS-988 shape (multiline text) is sent as separate paragraphs and breaks.
- **Verify:** `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR UV_PROJECT_ENVIRONMENT=/tmp/nexus-073-venv uv run pytest -q -p no:cacheprovider plugins/jira` passes; full checks pass; `uv.lock` committed.
- **Outcome:** `adf.py` with the tracer converter; `_adf` removed; the three write paths use
  `markdown_to_adf`; description/comment limit 32,767 (`_MAX_BODY`); D16 applied;
  `markdown-it-py>=3` declared and `uv.lock` updated (+2 lines). The skill's write limits were
  updated here, because the limits changed in this milestone. `plugins/jira`: 48 passed; full
  suite 2,141 passed; ruff check and format clean.

### [x] M2 — Rich Markdown is written as valid ADF

- **Approach:** extend `markdown_to_adf` with headings, marks (D10), links and images (D9),
  lists, code blocks, quotes, rules, tables and panels (D7), then the D8 normaliser driven by
  the allowed-children table. Add the test validator. Update the skill's write sections and
  the README's "How it works".
- **Test seam:** `markdown_to_adf()` with exact-ADF assertions; the validator over the corpus.
- **Edge cases:** empty list item → empty paragraph; empty table cell → empty paragraph;
  ragged table rows → padded; ordered list start 0 and 7; alert with no body → empty panel
  paragraph; D19 nesting stress.
- **Deliverable:** a Markdown plan with headings, lists, a table and a panel is sent as the
  matching ADF.
- **Verify:** `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR UV_PROJECT_ENVIRONMENT=/tmp/nexus-073-venv uv run pytest -q -p no:cacheprovider plugins/jira` passes; full checks pass.
- **Outcome:** `markdown_to_adf` covers headings, marks, links, images, lists, code blocks,
  quotes, rules, tables and panels, with the `ALLOWED`-driven D8 normaliser. Tests assert
  exact ADF per construct, run the structural validator over a 12-item corpus and the
  nesting stress cases, and confirm text survives fallback. `plugins/jira`: 78 passed; full suite 2,171 passed. Skill
  "Formatting descriptions and comments" section and README `adf.py` entry added. See the
  M2 deviation.

### [x] M3 — Reads return Markdown and round trips are stable

- **Approach:** `adf_to_markdown` per D2, D3, D11, D12, D14, D15, D17, D18; replace `adf_text`
  in `_detail`, `formatting.comment` and `add_comment`. Update the skill's output schema and
  add the Jira-only-content warning.
- **Test seam:** `adf_to_markdown()`; `get_issue` with a patched `jira_client`; the round-trip
  property over the corpus.
- **Deliverable:** `get_issue` returns a formatted Markdown description that saves back to the
  same ADF.
- **Verify:** `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR UV_PROJECT_ENVIRONMENT=/tmp/nexus-073-venv uv run pytest -q -p no:cacheprovider plugins/jira` passes; full checks pass.
- **Outcome:** `adf_to_markdown` renders every written node plus expands, mentions,
  statuses, dates, emoji, cards, media, task lists, Jira-editor tables (`<br>` cells, empty
  header) and unknown nodes; raw newlines in old text nodes read as line breaks; it never
  raises (malformed input, 5,000-level nesting). `_detail`, `formatting.comment` and
  `add_comment` use it; `adf_text` removed. A fuzzer (100,000+ random inputs during the build)
  drove fixes in both directions until the round trip was stable; a seeded 3,000-input fuzz
  test is kept. Skill schema, read-format section and re-save warning added; README updated.
  `plugins/jira`: 121 passed; full suite 2,214 passed; ruff clean. See the M3 deviation.

## Deviations

- M2: `markdown-it-py` silently drops content nested past `maxNesting` (default 20), and its
  recursive parser overflows the stack on ~16,000 levels. `maxNesting` is raised to 100;
  input that reaches the limit, or raises `RecursionError`, is converted as plain paragraphs
  with line breaks. This keeps D19 (never raises) and no content loss. Scope unchanged.
- M3: round-trip stability needed the writer to produce one canonical form, so `markdown_to_adf`
  now also drops whitespace at line edges and line breaks at paragraph edges (Jira does not
  show them), keeps emphasis off edge whitespace, de-duplicates repeated marks, turns line
  breaks inside setext headings into spaces, drops empty paragraphs left by degradation, keeps
  only plain code-fence language names (`[A-Za-z0-9_+#.-]`), and gives an empty link `href`
  of `#`. Behaviour visible in Jira is unchanged except that invisible whitespace is trimmed.

## Risks

- Jira may count ADF JSON size against 32,767, so large formatted text might still 400.
  Needs a live test on an authorised issue.
- Jira's ADF validator may reject a shape the test validator allows; the allowed-children
  table is built from Atlassian's node docs, not from live calls.
- Relative link targets (`[x](docs/a.md)`) are sent as-is; Jira may reject or mis-render them.
- Lists nested past ~50 levels make the whole document plain lines (text kept).

## Review

- Plan review: 1 pass. Fixed: Objective claim narrowed to plugin-written formatting plus a
  skill warning; M1 split into a thin tracer (M1) and rich constructs (M2); Verify uses the
  baseline env prefix; doc ACs cover the schema text and README; added D16 (whitespace),
  D17 (Jira-authored tables), D18 (unsupported marks), D19 (never raises); D12 limited to
  inline text; Decisions reordered; `uv lock` added to M1. No rejections. No ❌, so no second
  pass.
- Code review: 1 round, `general` and `risk` (risk ran: model-supplied input, provider
  payloads and a changed tool output contract). Fixed: empty-`src` images and empty links
  produced `href: ""` (now plain text; images link only `http(s)` sources, so `data:` URIs
  no longer become links); table cells dropped media, smart links, expand titles and task
  state on read (cells now keep them, and cell line breaks become `<br>` instead of ending the
  row); reader was quadratic on long runs of short emphasis spans (2.2 s at the cap; now
  linear via precomputed run lengths) and writer quadratic in links per paragraph (now a
  running length); `adf_to_markdown` raised on non-string code text (now never raises);
  validator imported production `ALLOWED` (now a literal copy of the plan table) and had a
  dead clause; three copies of the text-merge loop (now `_merge`); `_flat_inline` renamed
  `_degraded_inline` to separate it from the reader's `_flat`; limit test now covers
  `update_issue` and asserts full-length payloads; skill warns that escaped read-back can
  exceed 32,767 near the cap. Added regression tests for each, plus a size test at the cap
  (< 1 s per conversion). Answered: a list nested past the parser limit (~50 levels) turns the
  whole document into plain lines — accepted, because no text is lost and real descriptions
  never nest that deep. Not changed: commit subjects lack a breaking-change marker for the
  read format change; the PR body states it instead of rewriting history. Href schemes: markdown-it
  already blocks `javascript:`, `vbscript:`, `file:` and non-image `data:`; whether Jira accepts
  relative hrefs is unverified (Risks). No ❌, so no second round.
- Merge risk: Medium: the tool's description and comment format changes in both directions,
  and Jira's acceptance of the generated ADF is untested live; a rejected shape fails one
  write with a `JiraValidationError` (no partial write), and the change is one revertable PR.

## Outcome

Delivered: `plugins/jira/adf.py` converts Markdown to ADF on write (descriptions and
comments, up to 32,767 characters) and ADF to Markdown on read, with formatting the plugin
writes surviving a round trip. The skill and README document the contract.

Evidence: full suite 2,222 passed, 1 warning (baseline 2,126); `plugins/jira` 129 passed;
`ruff check` and `ruff format --check` clean; seeded fuzz test (3,000 inputs) plus 100,000+
ad-hoc fuzz inputs round-trip and validate; worst-case inputs at the cap convert in < 1 s.

Not verified: Jira's acceptance and rendering of the generated ADF (no authorised test
issue); whether Jira counts ADF JSON or text against 32,767.

PR: pending
CI: pending

## Follow-ups

- Re-save JS-988, JS-989 and JS-990 once rendering is verified (needs approval).
- Live rendering check on a test issue (needs approval).
