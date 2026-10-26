# 074 — Environment section from one markdown template

Status: building
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

- Env for every command: `export UV_PROJECT_ENVIRONMENT=$PWD/.venv ARCHIE_PERSONA_DIR=$PWD/persona ARCHIE_PLUGINS_DIR=$PWD/plugins`. The container's defaults point
  `UV_PROJECT_ENVIRONMENT` at the running agent's venv and `ARCHIE_PERSONA_DIR` at the main
  checkout's `persona/`; without the overrides tests read the wrong prompts and `uv sync`
  repoints the live agent (it did once; restored with the entrypoint's
  `cd /opt/archie && uv sync --package archie-agent --no-dev`).
- Check commands: `uv run pytest -q`, `uv run ruff check .`,
  `uv run ruff format --check <touched files>`
- Setup: `uv sync`
- Setup leftovers: `.venv`
- Failing before this work: none (2,139 passed)
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Template engine | `re.sub(r"\{(\w+)\}")` over a flat dict, one pass | Jinja2; `str.format` | User: Jinja2 is overkill. `str.format` breaks on literal braces in markdown and allows attribute access | user |
| D2 | Where lists render | Python pre-renders `{tools}` and `{paths}` | loops in the template | User's design. Without loops the renderer is one `re.sub`, so the template syntax stays two rules (placeholder, line guard) | user |
| D3 | Paths source | One fixed `PATHS` tuple in `environment.py` | frontmatter `mounts:`; two tables | User's direction. Paths are tied to the orchestrator's fixed mounts (`docs/architecture.md` mount table), not to the image, so they don't change when a tool is added; the two existing tables differ only in description wording | user |
| D4 | Unknown placeholder | Raise `ValueError` naming it | Leave literal text | A typo would otherwise ship `{os}` into every prompt; a test of the shipped template catches it | self |
| D5 | Optional lines (container note, sudo note, section headings) | Line guard: a line starting with `{?name}` is kept, minus the guard, only when value `name` is non-empty. Guards run first, line by line; then placeholders. An unknown guard name, or `{?` anywhere but the start of a line, raises `ValueError`. `container`/`sudo` are guard-only values (`"yes"`/`""`); as plain placeholders they would render "yes", which is allowed but unused. Runs of blank lines then collapse to one | Python-held sentences in `{container}`/`{sudo}`; full conditionals | Keeps every sentence and heading in the template (the Objective) with one line-level rule instead of block syntax | self |
| D6 | Tool key | Key is the binary name checked on PATH; no `binary:` field | separate `binary:` | `python` is on PATH in the image (`/usr/local/bin/python`), so no runtime needs an alias. In the agent process it resolves to `/opt/archie/venv/bin/python`, the same 3.13 interpreter the user's code runs with `uv`; acceptable | self |
| D7 | Version field | Optional `version_args` (string, split with `shlex`), run as `<key> <args>`; shown as major.minor | `version_cmd` full command; versions for every tool | Arguments only avoid repeating the binary. Only `python`, `node` (`--version`) and `go` (`version`) set it, so startup cost stays at today's three calls | self |
| D8 | Node major-only special case | Drop it; all versions major.minor | keep per-binary rule | Removes a hidden rule; versions only change when the image is rebuilt | self |
| D9 | Runtime companions (`uv`, `npm`) | Ordinary tools in the `Runtimes` category | `RUNTIME_COMPANIONS` table | One mechanism for every binary | self |
| D10 | Rendered tool list | Per category: `*Category*:` then `- \`tool\` (version) — note` per installed tool, categories in first-appearance order | today's comma lists plus a separate Tool notes block | User's sketch; one place per tool | user |
| D11 | Which paths show | An entry shows when it is a mount point, or it exists and an earlier `PATHS` entry that is its ancestor is shown | any mounted ancestor; exact mount only | Lets `~/.nexus/sessions` appear under the `~/.nexus` mount without treating a dev host's `/home` filesystem as host-mounted | self |
| D16 | Self paths presentation | `/opt/archie/*` become `PATHS` entries in the one paths list under the heading "Host-mounted (persistent; everything else is lost when the session ends):", described "this agent's own code — don't edit unless asked", "this agent's own prompts and skills — don't edit unless asked", "this agent's plugins — don't edit unless asked" | separate prose line | The user's combined list. They are host-mounted, so the heading stays true | user |
| D12 | Session logs location | `~/.nexus/sessions` entry in `PATHS` ("session logs (JSONL)") | `{ARCHIE_HOME}/logs` line | Logs are in `sessions/`; `logs/` holds orchestrator access/error logs. Fits the paths list | self |
| D13 | Template errors (missing file, bad frontmatter, bad tool entry) | Raise at prompt build; messages prefixed `environment.md:` and naming the key, e.g. `environment.md: tools.scalr: unknown key 'notes'`, `environment.md: unknown placeholder {nope}` | fall back to an empty section | Matches `_load_prompt` for other core fragments; a broken template fails the first session visibly instead of silently dropping the section | self |
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
- MUST raise `ValueError` naming an unknown placeholder or guard, and for a tool entry
  without a string `category` or with unknown keys.
  - AC: tests for `{nope}`, `{?nope}`, a mid-line `{?sudo}`, missing category, `notes:` typo.
- MUST keep a `{?name}` line only when `name` is non-empty, and leave no run of more than
  one blank line.
  - AC: test with `sudo=False`, not in a container, no tools, no paths: no blank-line runs,
    and none of "sudo", "docker", "Preinstalled", "Host-mounted" in the output.
- MUST insert placeholder values verbatim (no second expansion).
  - AC: test with a note containing `{user}` renders `{user}` literally.
- MUST render `{paths}` from one `PATHS` list per D11, as `- \`path\` — description`.
  - AC: test with mounts controlled: `~/.nexus/sessions` shown under a `~/.nexus` mount;
    path not under a mount omitted; `~/.agents` omitted when only `/home` (not a `PATHS`
    entry) is mounted.
- MUST keep the probe to once per process and the output deterministic.
  - AC: existing caching and determinism tests, adapted.
- MUST ship a template that renders with every placeholder known and every tool entry valid,
  carrying today's four notes unchanged except `pandoc` (dropped: it adds nothing), and this
  `scalr` note (verified against tillo.scalr.io in this session):
  `` global options go before the command (`scalr -format=table get-workspaces`); table
  shows a few columns, default JSON has every field. `-include=environment` expands
  related records, which are otherwise IDs only. ``
  - AC: test renders the shipped template with all tools available; asserts the `scalr`,
    `agent-browser`, `tofu` and `pdftotext` notes verbatim, a bare `- \`pandoc\`` line, and
    `go` `version_args: version`.
- MUST remove `tool-notes.md`, `TOOL_CATEGORIES`, `RUNTIMES`, `RUNTIME_COMPANIONS`,
  `PERSISTENT_PATHS`, `ARCHIE_SELF_PATHS`.
  - AC: `grep` for those names and `tool-notes` in `agent/`, `persona/` and `tests/` returns
    nothing.
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
  section text with placeholders `{os_name}`, `{arch}`, `{user}`, `{uid}`, `{home}`,
  `{workspace_dir}`, `{tools}`, `{paths}`, `{exec_python}`, and guards `{?container}`, `{?sudo}`,
  `{?tools}`, `{?paths}`.
- `persona/prompts/tool-notes.md`: deleted.
- `agent/src/archie_agent/environment.py` (rewritten):
  - `PATHS: tuple[tuple[str, str], ...]`: `/workspace`, `~/.nexus`, `~/.nexus/sessions`,
    `~/.agents`, `/opt/archie/agent`, `/opt/archie/persona`, `/opt/archie/plugins`, with
    descriptions (self paths per D16).
  - `ToolSpec` (frozen dataclass: `name`, `category`, `version_args: str | None`,
    `note: str | None`); `parse_template(text) -> (tuple[ToolSpec, ...], body)`.
  - `EnvironmentFacts`: `in_container, os_name, arch, user, uid, home, sudo`,
    `tools: tuple[tuple[str, str | None], ...]` (installed name, version),
    `paths: tuple[tuple[str, str], ...]`. `cwd` and `available` are removed (unused).
  - `probe(specs)`; `facts(specs)` cached with `functools.cache` (specs are hashable).
  - `render(template_text, workspace_dir, exec_python, env=None) -> str`: parses the
    template; `env=None` means `facts(specs)` for the parsed specs. Tests pass `env`
    directly; the caching test monkeypatches `environment.probe` with a one-argument fake.
  - `~/.nexus` description drops "sessions" (now its own entry).
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
- **Verify:** with the Baseline env exported, `uv run pytest -q tests/test_environment.py
  tests/test_prompt.py` passes; `uv run python -c "from archie_agent.prompt import
  _build_environment; print(_build_environment('m', '/workspace'))"` prints a `- \`scalr\` —`
  line, `- \`python\` (3.13)`, and `- \`~/.nexus/sessions\` — session logs (JSONL)`; full
  suite and ruff pass.
- **Outcome:**

## Deviations

## Risks

- Until this merges, the main checkout's `persona/` has `tool-notes.md` and no
  `environment.md`, while the live agent runs the main checkout's code, so nothing breaks
  in this container. Review subagents read the worktree but run on the live agent. After
  merge, code and persona update together; a container whose persona mount lags its agent
  source would fail prompt build (D13), which is the intended visible failure.

- The rendered tool list is longer (one line per tool, about 30 lines instead of 5). It is
  static, so it costs cached prompt tokens only.

## Review

- Plan review: 1 pass. Fixed: ❌ container/sudo sentences and headings had no home (D5 line
  guard); ❌ exact `scalr` note; ⚠️ heading behaviour AC, Verify command, D3/D2 reasons, D11
  ancestor rule (now PATHS-only), D6 venv python, D16 self-path presentation; ℹ️ go
  `version_args`, removed `cwd`, `render` defaults and caching test, error message format,
  duplicate "sessions", removal grep includes `tests/`. Rejected: none.
- Plan review pass 2: ❌ tests would load the main checkout's persona: fixed with the
  Baseline env (and found `uv sync` had repointed the live agent venv; restored). ⚠️
  persona/agent mismatch recorded in Risks; D16 descriptions fixed; D5 guard order, unknown
  guard and overlap specified. ℹ️ shipped-notes AC and `/home` mount AC added. Rejected:
  none.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Add the "learn the shape of unfamiliar JSON" exec pattern to `tools-strategy.md`.
