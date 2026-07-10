# 010 — Skills Functionality

## Objective

Add a skills system that discovers YAML-frontmattered Markdown skill files at session
start, presents a catalog in the system prompt, and allows the model to load skill
bodies on demand via a host-side `skill` tool. Loaded skills are injected into the
system prompt for all subsequent LLM requests in the session.

## Context

Plan 008 introduced `build_system_prompt()` that assembles sections (identity, environment,
tools). Currently the prompt is computed once at harness construction — skills require it
to evolve mid-session as skills are loaded. The `ToolRegistry` in `tools.py` holds
`ToolSpec` objects; currently only `exec` is registered via `create_registry()` in
`exec/tool.py`. The harness dispatches tool calls by name in `_execute_tool()`.

Nextgen reference: `src/archie/skills.py` (discovery) and `src/archie/tools/skill.py`
(tool handler). Skills are YAML-frontmattered Markdown files at
`~/.agents/skills/*/SKILL.md` and `~/.archie/skills/*/SKILL.md`.

## Requirements

- MUST discover skills from `~/.agents/skills/*/SKILL.md` and
  `~/.archie/skills/*/SKILL.md` at session start
  - AC: Skills in all directories are discovered; archie specific skills override general skills
    with the same name
- MUST parse YAML frontmatter to extract `name` and `description`
  - AC: Valid frontmatter produces a `SkillEntry`; malformed files are skipped with warning
- MUST render a skills catalog in the system prompt listing names and descriptions
  - AC: Prompt contains a `<skills>` section when catalog is non-empty
- MUST omit the skills section when no skills are discovered
- MUST register a `skill` host-side tool in the `ToolRegistry` alongside `exec`
  - AC: LLM receives tool configs for both `exec` and `skill`
- MUST load a skill's body into the system prompt when model calls `skill` with `name`
  - AC: After load, next LLM request's system prompt includes `<skill name>...</skill>`
- MUST persist loaded skills for the session (never evicted)
- MUST rebuild the system prompt per-turn incorporating loaded skills
  - AC: `run_loop()` receives updated `system` string each invocation
- MUST support reading reference files via `file` parameter
  - AC: `skill {"name":"x","file":"references/foo.md"}` returns file content
  - AC: Paths resolving outside the skill directory are rejected
- MUST indicate already-loaded skills in the catalog section
- MUST return an error with available names when loading an unknown skill
- MUST return a no-op message when loading an already-loaded skill
- SHOULD list reference files after successful load
- SHOULD handle missing skill directories gracefully (empty catalog, no crash)
- MAY support additional frontmatter fields in future (silently ignored)

## Technical Design

### 1. Discovery module: `agent/src/archie_agent/skills.py`

```python
@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    path: Path  # absolute path to SKILL.md

def discover_skills() -> dict[str, SkillEntry]:
    """Scan user skill directories, return catalog keyed by name."""
```

Scans `~/.agents/skills/` first (lower priority), then
`~/.archie/skills/` (higher priority, overwrites on collision). One level
deep. Uses `yaml.safe_load` for frontmatter. `pyyaml` is already a dependency of
`archie_shared`.

### 2. Skill tool handler (in `skills.py`)

```python
def create_skill_tool(
    catalog: dict[str, SkillEntry],
    loaded_skills: list[tuple[str, str]],
) -> ToolSpec:
```

Returns a `ToolSpec(name="skill", ...)` with async handler supporting:
- `name` only → load body, append to `loaded_skills`, list reference files
- `name` + `file` → read reference file (validate containment)

Schema: `{"name": {"type": "string"}, "file": {"type": "string"}}`, `required: ["name"]`.

### 3. Prompt integration

`build_system_prompt()` gains optional kwargs:

```python
def build_system_prompt(
    model_name: str,
    workspace_dir: str = str(WORKSPACE),
    *,
    catalog: dict[str, SkillEntry] | None = None,
    loaded_skills: list[tuple[str, str]] | None = None,
) -> str:
```

New sections after tools:
- `_build_skills_catalog(catalog, loaded_skills)` → `<skills>` listing
- `_build_loaded_skills(loaded_skills)` → `<skill name>body</skill>` sections

### 4. Harness changes — per-turn prompt rebuild

`AgentHarness.__init__` changes:
- Receives `model_name: str` instead of `system_prompt: str`
- Runs `discover_skills()` → stores `self._skill_catalog`
- Creates `self._loaded_skills: list[tuple[str, str]] = []`
- Registers skill tool on registry
- New `_build_prompt() -> str` called per-turn in `handle_message()`

### 5. Tool registration

Harness builds full registry:
- `exec` from existing `create_registry()`
- `skill` from `create_skill_tool(catalog, loaded_skills)`

### 6. Container mounts

- `~/.agents/` → mounted read-only into container

## Milestones

### 1. Skill discovery module

**Approach:**
Create `agent/src/archie_agent/skills.py` with `SkillEntry` dataclass and
`discover_skills()` function. Scans `~/.agents/skills/` (lower priority) then
`~/.archie/skills/` (higher priority). Parse YAML frontmatter, skip malformed files.
No `project_dir` parameter — both paths are user-level.

**Tasks:**
- Create `agent/src/archie_agent/skills.py` with `SkillEntry` and `discover_skills()`
- Implement `_scan_directory()` and `_parse_skill_file()`
- Create `tests/test_skills.py` covering: discovery from both paths, override on name
  collision (~/.archie wins over ~/.agents), malformed files, missing dirs

**Edge Cases:**
- Only one `---` delimiter → skip with warning
- Frontmatter not a dict → skip
- Missing name/description → skip
- Permission errors → skip, don't crash
- Neither directory exists → empty dict, no error

**Deliverable:** `discover_skills()` returns a complete catalog from fixture directories.

**Verify:** `uv run pytest tests/test_skills.py -v` passes.

---

### 2. Skill tool handler

**Approach:**
Add `create_skill_tool()` to `skills.py`. Closure captures catalog and loaded_skills.
Supports load and read modes.

**Tasks:**
- Implement `create_skill_tool(catalog, loaded_skills) -> ToolSpec`
- Load mode: validate name, check duplicates, extract body, append, list refs
- Read mode: resolve file path, validate containment, read content
- Define schema
- Create `tests/test_skill_tool.py`

**Edge Cases:**
- Already loaded → no-op message
- Unknown name → error with available names
- Path traversal in file param → rejected
- Binary reference file → error
- Skill body is whitespace → stripped to empty

**Deliverable:** `create_skill_tool()` returns a working `ToolSpec`.

**Verify:** `uv run pytest tests/test_skill_tool.py -v` passes.

---

### 3. Prompt integration — skills sections

**Approach:**
Extend `build_system_prompt()` with catalog and loaded_skills params. Add section
builders. Maintain backward compat (params are optional).

**Tasks:**
- Add params to `build_system_prompt()` signature
- Implement `_build_skills_catalog()` and `_build_loaded_skills()`
- Integrate into section assembly (after tools)
- Update existing tests, add new tests

**Deliverable:** Prompt renders skills sections when data provided; backward-compatible.

**Verify:** `uv run pytest tests/test_prompt.py -v` passes.

---

### 4. Harness wiring — per-turn prompt rebuild and tool registration

**Approach:**
Modify `AgentHarness` to own skill state, rebuild prompt per-turn, register skill tool.
Update `app.py` to pass `model_name` instead of pre-built prompt.

**Wiring:**
- `__init__` receives `model_name: str` (replacing `system_prompt: str`)
- Runs `discover_skills()` (no args — scans user-level paths), stores catalog
- Creates loaded_skills list, registers skill tool
- `_build_prompt()` called per-turn before `run_loop()`
- `app.py` passes `model_name=model.name`

**Tasks:**
- Change `AgentHarness.__init__` signature: replace `system_prompt` with `model_name`
- Add `discover_skills()` call in `__init__`
- Add `_loaded_skills` list, register skill tool
- Add `_build_prompt()` method
- Update `handle_message()` to use per-turn prompt
- Update `app.py` to pass `model_name=model.name`
- Update all test fixtures constructing harnesses
- Add integration test: skill load → prompt includes body on next turn

**Edge Cases:**
- No skills discovered → tool still registered (returns "no skills available")
- Multiple skill loads in one response → all appended, visible next iteration
- Skill mutated during current iteration → takes effect on next `run_loop()` call

**Deliverable:** Harness discovers skills, registers tool, rebuilds prompt per-turn.

**Verify:** `uv run ruff check && uv run pytest -q` — all pass.
