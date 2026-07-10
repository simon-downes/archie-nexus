"""Dynamic system prompt builder.

Assembles the system prompt from discrete sections: identity, environment, tools.
Each section is a pure function; the assembled prompt is a single string suitable
for the Bedrock `system` field.
"""

from __future__ import annotations

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

You have one external tool: `exec`. It runs Python code in a fresh subprocess
inside the workspace. Define `async def main()` — it will be awaited and its
return value captured.

**Use `exec` to complete a whole related step in ONE call:** search, filter, read
multiple files, compute, edit, and return only the useful result. Don't make many
small calls for independent reads or searches. Make another call when new results
change the plan, or after edits that need validation.

### Execution model

- Each `exec` call starts fresh — variables, imports, and state do not persist.
- Pre-injected (no import needed): `asyncio`, `os`, `json`, `re`, `Path`, plus the
  helper functions `read`, `write`, `edit`, `grep`, `glob`, `shell`.
- Full Python stdlib is available via `import`.
- Batch independent I/O with `asyncio.gather`; run dependent steps sequentially.
- Return concise structured data. Filter in Python; don't dump whole files unless
  necessary.
- Results come back as `return: <json>`, `return (repr): ...`, `stdout:`,
  `stderr:`, or `error: <Type>: <message>`.

### Never use shell for file work

`shell` is a last resort. Do NOT use it to read, search, list, or modify files —
use the helpers instead. They return line numbers, structured data, and diffs
that shell output lacks, and they enforce path safety.

- `cat`/`head`/`tail` → `read`
- `grep`/`rg` → `grep`
- `find`/`ls` → `glob`
- `sed`/redirection (`>`) → `edit`

Reserve `shell` for tests, builds, package commands, and git.

### Helper behavior

- Paths are relative to the workspace root (e.g. `src/app.py`).
- `read(path)` returns line-numbered text like `    1| content`; `read(path, raw=True)`
  returns plain text.
- `grep(pattern, include="*.py")` returns `[{"path", "line", "text"}, ...]`.
- `glob(pattern)` returns a sorted list of paths.
- `edit(...)` returns a unified diff.
- `shell(...)` returns `{"stdout", "stderr", "exit_code"}` — always check `exit_code`;
  non-zero is data, not an exception.
- Helper errors raise exceptions. Catch them only when recovery is useful.

### Working rules

- Inspect relevant files before editing, unless creating a new file or the
  requested overwrite is unambiguous.
- Prefer `edit` for existing files — it shows a diff.
- After edits, run the narrowest relevant validation first, then broader tests if
  needed.
- Return only the facts, diffs, errors, or test results needed for the next decision.

### Patterns

**Search, filter, and read in one call:**
```python
async def main():
    hits = await grep(pattern="TODO", include="*.py")
    files = sorted({h["path"] for h in hits})
    contents = await asyncio.gather(*(read(path=f) for f in files[:5]))
    return {"files": files, "contents": contents}
```

**Edit and validate:**
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

    Returns:
        Complete system prompt string for the Bedrock system field.
    """
    sections = [
        _IDENTITY,
        _build_environment(model_name, workspace_dir),
        _build_tools(),
    ]

    # Skills sections (only when catalog is non-empty)
    if catalog:
        sections.append(_build_skills_catalog(catalog, loaded_skills or []))

    # Loaded skill bodies
    if loaded_skills:
        sections.append(_build_loaded_skills(loaded_skills))

    return "\n\n".join(sections)
