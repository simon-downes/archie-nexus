# 074 — Environment section from one markdown template

Status: planning
Branch: build-it/074-environment-template (base: main @ 75710b2)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/074-environment-template

## Objective

The system prompt's Environment section is built from Python string literals and hardcoded
tables in `agent/src/archie_agent/environment.py`, plus `persona/prompts/tool-notes.md`.
Adding a preinstalled binary takes edits in three places (Dockerfile, `TOOL_CATEGORIES`,
`tool-notes.md`), the coupling is undocumented, and a note for a binary missing from
`TOOL_CATEGORIES` is silently dropped. When done, one template file holds the section's
text, the tool list and the tool notes; Python only probes the container and fills
placeholders.

## Context

User's idea: "environment.py and tool-notes.md should really just be environment.md which
is a template where certain runtime info can be filled in"; a markdown file with YAML
frontmatter listing tools (`category`, version command, `note`); simple `{name}`
placeholders filled from probed facts, with `{tools}` and `{paths}` pre-rendered; no Jinja2.
`{paths}` renders `- \`path\` — description` and comes from one fixed list in
`environment.py` that combines `PERSISTENT_PATHS` and `ARCHIE_SELF_PATHS`. Aim: simplify
the structure, reduce duplication, make it easier to understand and maintain.

Discovery:
- `prompt._build_environment()` loads `tool-notes.md` (empty on `OSError`) and calls
  `environment.render(environment.facts(), workspace_dir, notes, PYTHON)`. It is used by
  both `build_system_prompt_structured()` and `build_subagent_prompt()`; the section is
  part of the cached static prompt and must not vary by model.
- `environment.facts()` is `functools.cache`d: one probe per process.
- `probe()` checks `TOOL_CATEGORIES` names with `shutil.which`, runs `--version`/`version`
  for python3/node/go (5 s timeout each), appends `uv`/`npm` as runtime companions, reads
  `/proc/self/mounts` for `PERSISTENT_PATHS` and `ARCHIE_SELF_PATHS`, detects sudo with
  `sudo -n true` and the container via `/.dockerenv`.
- Prompt fragments load with `prompt._load_prompt(name)` from `persona/prompts/`; missing
  core fragments (e.g. `identity.md`) raise.
- Frontmatter parsing exists in `agent/src/archie_agent/skills.py` (`split("---", 2)` +
  `yaml.safe_load`) and `shared/.../brain.py::_split_frontmatter` (private). PyYAML comes
  through `archie-shared`.
- No doc mentions `persona/prompts`, `TOOL_CATEGORIES` or the Preinstalled list.
- Tests: `tests/test_environment.py` (render, notes filtering, caching, live probe) and
  `tests/test_prompt.py` (`_build_environment` workspace/model checks).

## Baseline

- Check commands: `ARCHIE_PLUGINS_DIR=$PWD/plugins uv run pytest -q`, `uv run ruff check .`,
  `uv run ruff format --check <touched files>`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none in the worktree (2,139 passed with `ARCHIE_PLUGINS_DIR` set)
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Template engine | `re.sub(r"\{(\w+)\}")` over a flat dict, one pass | Jinja2; `str.format` | User: Jinja2 is overkill. `str.format` breaks on literal braces in markdown and allows attribute access | user |
| D2 | Where lists render | Python pre-renders `{tools}` and `{paths}` | loops in the template | User's design; keeps the substitution trivial | user |
| D3 | Paths source | One fixed `PATHS` tuple in `environment.py` | frontmatter `mounts:`; two tables | User's direction | user |
| D4 | Unknown placeholder | Raise `ValueError` naming it | Leave literal text | A typo would otherwise ship `{os}` into every prompt; a test of the shipped template catches it | self |
| D5 | Optional lines (container, sudo) | Placeholder alone on a line that renders empty removes the line; then runs of blank lines collapse to one | conditionals in the template | Keeps the template plain markdown | self |
| D6 | Tool key | Key is the binary name checked on PATH; no `binary:` field | separate `binary:` | `python` is on PATH in the image (`/usr/local/bin/python`), so no runtime needs an alias | self |
| D7 | Version field | Optional `version_args` (string, split with `shlex`), run as `<key> <args>`; shown as major.minor | `version_cmd` full command; versions for every tool | Arguments only avoid repeating the binary. Only runtimes set it, so startup cost stays at today's three calls (`uv`, `npm` gain none) | self |
| D8 | Node major-only special case | Drop it; all versions major.minor | keep per-binary rule | Removes a hidden rule; versions only change when the image is rebuilt | self |
| D9 | Runtime companions (`uv`, `npm`) | Ordinary tools in the `Runtimes` category | `RUNTIME_COMPANIONS` table | One mechanism for every binary | self |
| D10 | Rendered tool list | Per category: `*Category*:` then `- \`tool\` (version) — note` per installed tool, categories in first-appearance order | today's comma lists plus a separate Tool notes block | User's sketch; one place per tool | user |
| D11 | Which paths show | An entry shows when the path exists and it, or an ancestor other than `/`, is a mount point | exact mount point only; exists only | Lets `~/.nexus/sessions` (under the `~/.nexus` mount) appear, and keeps the "host-mounted, persistent" claim true | self |
| D12 | Session logs location | `~/.nexus/sessions` entry in `PATHS` ("session logs (JSONL)") | `{ARCHIE_HOME}/logs` line | Logs are in `sessions/`; `logs/` holds orchestrator access/error logs. Fits the paths list | self |
| D13 | Template errors (missing file, bad frontmatter, bad tool entry) | Raise at prompt build | fall back to an empty section | Matches `_load_prompt` for other core fragments; a broken template fails the first session visibly instead of silently dropping the section | self |
| D14 | Dockerfile/template drift test | No test; document the two-step procedure | string-match test | Package names differ from binaries (`mariadb-client`→`mysql`, `poppler-utils`→`pdftotext`, `redis-tools`→`redis-cli`), so a test needs a mapping field: the duplication this build removes | self |
| D15 | Template documentation | YAML comments in the frontmatter (stripped before rendering) + a paragraph in `docs/architecture.md` | HTML comment in the body | A body comment would be rendered into the prompt | self |

## Requirements

- MUST render the Environment section from `persona/prompts/environment.md`: frontmatter
  `tools:` mapping, body with `{name}` placeholders.
  - AC: a test renders a template with custom body text and tools and gets that text back
    with values filled.
- MUST list only tools found on PATH, grouped by `category`, with version when
  `version_args` is set and the probe finds one, and with `note` when set.
  - AC: test with `shutil.which` controlled: absent tool omitted; empty category omitted;
    line format `- \`tofu\` — note`, `- \`python\` (3.13)`.
- MUST raise `ValueError` naming an unknown placeholder, and for a tool entry without a
  string `category` or with unknown keys.
  - AC: tests for `{nope}`, missing category, `notes:` typo.
- MUST remove a line that is only a placeholder rendering empty, and leave no run of more
  than one blank line.
  - AC: test with `sudo=False`, not in a container, no tools, no paths: no blank-line runs,
    no "sudo"/"docker" text.
- MUST insert placeholder values verbatim (no second expansion).
  - AC: test with a note containing `{user}` renders `{user}` literally.
- MUST render `{paths}` from one `PATHS` list per D11, as `- \`path\` — description`.
  - AC: test with mounts controlled: `~/.nexus/sessions` shown under a `~/.nexus` mount;
    path not under a mount omitted.
- MUST keep the probe to once per process and the output deterministic.
  - AC: existing caching and determinism tests, adapted.
- MUST ship a template that renders with every placeholder known and every tool entry valid,
  including `scalr` with a note on global option order, `-format=table` and `-include`.
  - AC: test renders the shipped template; asserts `scalr` entry parses.
- MUST remove `tool-notes.md`, `TOOL_CATEGORIES`, `RUNTIMES`, `RUNTIME_COMPANIONS`,
  `PERSISTENT_PATHS`, `ARCHIE_SELF_PATHS`.
  - AC: `grep` for those names in `agent/` and `persona/` returns nothing.
- MUST document how to add a preinstalled tool (Dockerfile + one frontmatter entry).
  - AC: `docs/architecture.md` has the paragraph; template frontmatter has the format comment.

Out of scope: changing other prompt fragments; a Scalr skill; the JSON-shape exec pattern
for `tools-strategy.md` (separate follow-up); Dockerfile changes.

## Design

### Overview

`environment.md` = YAML frontmatter (tool list) + markdown body (layout). `environment.py`
parses it, probes once, builds a flat `dict[str, str]` of values, substitutes, tidies blank
lines.

### Code structure

- `persona/prompts/environment.md` (new): frontmatter `tools:`; body reproduces today's
  section text with placeholders `{container}`, `{os_name}`, `{arch}`, `{user}`, `{uid}`,
  `{home}`, `{workspace_dir}`, `{sudo}`, `{tools}`, `{paths}`, `{exec_python}`.
- `persona/prompts/tool-notes.md`: deleted.
- `agent/src/archie_agent/environment.py` (rewritten):
  - `PATHS: tuple[tuple[str, str], ...]`: `/workspace`, `~/.nexus`, `~/.nexus/sessions`,
    `~/.agents`, `/opt/archie/agent`, `/opt/archie/persona`, `/opt/archie/plugins`, with
    descriptions (self paths say "this agent's own code/prompts — don't edit unless asked").
  - `ToolSpec` (frozen dataclass: `name`, `category`, `version_args: str | None`,
    `note: str | None`); `parse_template(text) -> (tuple[ToolSpec, ...], body)`.
  - `EnvironmentFacts`: `in_container, os_name, arch, user, uid, home, sudo`,
    `tools: tuple[tuple[str, str | None], ...]` (installed name, version),
    `paths: tuple[tuple[str, str], ...]`.
  - `probe(specs)`; `facts(specs)` cached with `functools.cache` (specs are hashable).
  - `render(template_text, workspace_dir, exec_python, env=None) -> str`.
- `agent/src/archie_agent/prompt.py`: `_build_environment` loads `environment.md` with
  `_load_prompt` and calls `environment.render(...)`.
- Frontmatter split: local helper in `environment.py` following `skills.py::_parse_skill_file`
  (`startswith("---")`, `split("---", 2)`, `yaml.safe_load`). Not shared: `brain.py`'s
  helper is private to `shared` and returns `None` on errors, where D13 wants a raise.

### Error handling

Missing template → `OSError` from `_load_prompt` propagates. Bad YAML/frontmatter shape,
bad tool entry, unknown placeholder → `ValueError` with the file-relative reason. Version
probe failure or timeout (5 s) → no version shown, as today.

## Milestones

### [ ] M1 — Environment section rendered from environment.md

- **Approach:** Write `environment.md` reproducing today's output text with the D10 tool
  layout; rewrite `environment.py` per Code structure; switch `prompt._build_environment`;
  delete `tool-notes.md`; rewrite `tests/test_environment.py` around `render()` with
  `EnvironmentFacts` passed in and around `probe()` with `shutil.which`/mounts monkeypatched;
  keep `tests/test_prompt.py` environment tests passing. Add the `docs/architecture.md`
  paragraph after the "The image contains dependencies and system tools." paragraph.
  ⚠️ `_build_environment` must still ignore `model_name`.
- **Test seam:** `environment.render()`, `environment.probe()`, `prompt._build_environment()`.
- **Edge cases:** frontmatter missing → `ValueError`; `tools:` empty → no tools block;
  tool note with braces → verbatim.
- **Deliverable:** the agent's Environment section comes from `environment.md`; adding a
  tool is one frontmatter entry.
- **Verify:** `ARCHIE_PLUGINS_DIR=$PWD/plugins uv run pytest -q tests/test_environment.py
  tests/test_prompt.py` passes; rendering the shipped template in this container shows the
  tool list (including `scalr`) and paths; full suite and ruff pass.
- **Outcome:**

## Deviations

## Risks

- The rendered tool list is longer (one line per tool, about 30 lines instead of 5). It is
  static, so it costs cached prompt tokens only.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Add the "learn the shape of unfamiliar JSON" exec pattern to `tools-strategy.md`.
