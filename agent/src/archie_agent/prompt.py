"""Dynamic system prompt builder.

Assembles the system prompt from discrete sections: identity, environment, tools.
Each section is a pure function; the assembled prompt is a single string suitable
for the Bedrock `system` field.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from archie_agent.exec.tool import PYTHON
from archie_agent.exec.tools import get_tool_guidelines
from archie_agent.exec.tools.fs import WORKSPACE

if TYPE_CHECKING:
    from archie_agent.skills import SkillEntry

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

_IDENTITY = """\
You are Archie, a coding assistant running in a sandboxed container.

- Be concise and direct. No filler, no preamble.
- Use tools proactively — investigate before answering.
- Return structured data from exec; use print() for debug output only.
- Implement exactly what is asked — no more.
- A successful write or edit means the change is applied. Do not re-read to verify.
- When something fails, investigate the actual error before retrying."""


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _build_environment(model_name: str, workspace_dir: str) -> str:
    """Build the environment context section."""
    return f"""\
## Environment

- Model: {model_name}
- Workspace: {workspace_dir} (project root, mounted from host)
- Exec interpreter: {PYTHON}
- Container: isolated Docker (Debian), ephemeral — destroyed after session
- All file paths are relative to {workspace_dir} unless absolute"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_TOOLS_STRATEGY = """\
## Tools

You have native tools (`read`, `grep`, `glob`, `edit`, `write`, `shell`,
`web_fetch`, `web_search`, `code`) for single operations, plus `exec` for
multi-step Python code.

### When to use which

**Native tools** — prefer for single operations:
- Reading a file → `read`
- Searching file contents → `grep`
- Finding files → `glob`
- Editing a file → `edit`
- Creating/overwriting a file → `write`
- Running a command → `shell`
- Fetching a URL → `web_fetch`
- Searching the web → `web_search`
- Exploring code structure → `code`

**`exec`** — use when you need to:
- Chain multiple steps with data flowing between them
- Filter or transform large output before returning
- Loop over results or use conditional logic
- Batch independent I/O with `asyncio.gather` (pass `return_exceptions=True`
  when any call might fail, e.g. reading files that may not exist)

### Native tool behavior

- Paths are relative to the workspace root (e.g. `src/app.py`).
- `read` returns line-numbered text; `read(path, raw=True)` returns plain text.
- `grep` returns results grouped by file with line numbers, most recently modified first.
- `glob` returns a file list sorted by modification time (most recent first).
- `edit` returns a unified diff showing the change.
- `write` returns a confirmation with path and line count.
- `shell` returns `$ command\\n[exit: N]\\noutput` — always check the exit code.
- `code` returns a structural outline of symbols with line ranges.
- All tools raise exceptions on errors (path validation, file not found, etc.).
- A successful write or edit means the change is applied. Do not re-read to verify.

### Never use shell for file work

`shell` is a last resort. Do NOT use it to read, search, list, or modify files —
use the native tools instead. They return line numbers, structured data, and diffs
that shell output lacks, and they enforce path safety.

- `cat`/`head`/`tail` → `read`
- `grep`/`rg` → `grep`
- `find`/`ls` → `glob`
- `sed`/redirection (`>`) → `edit`

Reserve `shell` for tests, builds, package commands, and git.

### exec tool

`exec` runs Python code in a fresh subprocess inside the workspace. Define
`async def main()` — it will be awaited and its return value captured.

**Use `exec` to complete a whole related step in ONE call:** search, filter, read
multiple files, compute, edit, and return only the useful result.

- Each `exec` call starts fresh — variables, imports, and state do not persist.
- Pre-injected (no import needed): `asyncio`, `os`, `json`, `re`, `Path`, plus the
  helper functions `read`, `write`, `edit`, `grep`, `glob`, `shell`, `web_fetch`,
  `web_search`, `code`.
- Full Python stdlib is available via `import`.
- Batch independent I/O with `asyncio.gather`; run dependent steps sequentially.
  `gather` fails fast by default — one raised exception discards the whole
  batch. Pass `return_exceptions=True` when any call might fail (e.g. reading
  files that may not exist), then check each result with `isinstance(r,
  BaseException)`.
- Return concise structured data. Filter in Python; don't dump whole files unless
  necessary.
- Results come back as `return: <json>`, `return (repr): ...`, `stdout:`,
  `stderr:`, or `error: <Type>: <message>`.

### Patterns

**Search, filter, and read in one call (use exec):**
```python
async def main():
    hits = await grep(pattern="TODO", include="*.py")
    # grep returns a string — parse file paths from it if needed
    files = sorted({line.split(":")[0] for line in hits.split("\\n") if "|" in line})
    # return_exceptions=True so one missing/unreadable file doesn't sink the batch
    results = await asyncio.gather(
        *(read(path=f) for f in files[:5]), return_exceptions=True
    )
    contents = {
        f: r for f, r in zip(files, results) if not isinstance(r, BaseException)
    }
    return {"files": files, "contents": contents}
```

**Edit and validate (use exec):**
```python
async def main():
    diff = await edit(path="src/utils.py", old="return a - b", new="return a + b")
    test = await shell("python -m pytest tests/test_utils.py -x")
    return {"diff": diff, "test": test}
```"""


def _build_tools() -> str:
    """Build the tools section: strategy + aggregated guidelines."""
    guidelines = get_tool_guidelines()

    parts = [_TOOLS_STRATEGY]

    if guidelines:
        bullet_list = "\n".join(f"- {g}" for g in guidelines)
        parts.append(f"\n\n### Guidelines\n\n{bullet_list}")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _build_skills_catalog(
    catalog: dict[str, SkillEntry],
    loaded_skills: list[tuple[str, str]],
) -> str:
    """Build the skills catalog section for the system prompt.

    Lists all available skills with their descriptions. Skills that are
    already loaded are marked with [loaded].
    """
    loaded_names = {name for name, _ in loaded_skills}
    lines = ["<skills>", "Available skills (use the `skill` tool to load):"]
    for name in sorted(catalog):
        entry = catalog[name]
        marker = " [loaded]" if name in loaded_names else ""
        lines.append(f"- {name}: {entry.description}{marker}")
    lines.append("</skills>")
    return "\n".join(lines)


def _build_loaded_skills(loaded_skills: list[tuple[str, str]]) -> str:
    """Build the loaded skills section — each skill body in a tagged block."""
    parts: list[str] = []
    for name, body in loaded_skills:
        parts.append(f'<skill name="{name}">\n{body}\n</skill>')
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Project context (AGENTS.md)
# ---------------------------------------------------------------------------


def _build_project_context(workspace_dir: str) -> str:
    """Build the project-context section from the workspace's AGENTS.md.

    Reads ``{workspace_dir}/AGENTS.md`` at prompt-build time and wraps its
    contents in an ``<agents.md>`` block. Returns an empty string if the file
    is absent, empty, or unreadable.
    """
    try:
        agents_md = (Path(workspace_dir) / "AGENTS.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    agents_md = agents_md.strip()
    if not agents_md:
        return ""
    return f"<agents.md>\n{agents_md}\n</agents.md>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(
    model_name: str,
    workspace_dir: str = str(WORKSPACE),
    *,
    catalog: dict[str, SkillEntry] | None = None,
    loaded_skills: list[tuple[str, str]] | None = None,
) -> str:
    """Assemble the full system prompt from sections.

    Args:
        model_name: Model identifier (e.g. "claude-sonnet-4-20250514").
        workspace_dir: Project root path inside the container.
        catalog: Optional skill catalog for rendering the skills section.
        loaded_skills: Optional list of (name, body) tuples for loaded skills.

    Reads ``{workspace_dir}/AGENTS.md`` at build time (if present) and includes
    it as an ``<agents.md>`` block for project-specific context.
    """
    sections = [
        _IDENTITY,
        _build_environment(model_name, workspace_dir),
        _build_tools(),
    ]

    # Skills sections (only when catalog is non-empty)
    if catalog:
        sections.append(_build_skills_catalog(catalog, loaded_skills or []))

    # Project context (AGENTS.md), between skills catalog and loaded skill bodies
    project_context = _build_project_context(workspace_dir)
    if project_context:
        sections.append(project_context)

    # Loaded skill bodies
    if loaded_skills:
        sections.append(_build_loaded_skills(loaded_skills))

    return "\n\n".join(sections)
