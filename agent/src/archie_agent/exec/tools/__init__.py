"""Exec tool functions for code-mode execution.

These are async functions that model code calls directly inside the runner
subprocess. They raise typed exceptions on errors (not return error strings).
The runner catches unhandled exceptions and surfaces them in the result envelope.
"""

from __future__ import annotations

# --- Exception hierarchy ---


class ToolError(Exception):
    """Base exception for all exec tool errors.

    The runner catches subclasses and surfaces them cleanly in the envelope
    (type + message, no full traceback). Unhandled non-tool exceptions
    get the full traceback treatment.
    """


class PathValidationError(ToolError):
    """Path is outside /workspace/ or uses disallowed traversal."""


class FileNotFoundError(ToolError):  # noqa: A001
    """File or directory does not exist."""


class BinaryFileError(ToolError):
    """Attempted to read a binary file as text."""


class EditError(ToolError):
    """Edit failed: old text not found or ambiguous match."""


class ContentTypeError(ToolError):
    """URL returned non-text content (images, PDFs, etc.)."""


class UnsupportedLanguageError(ToolError):
    """Language not supported or grammar unavailable."""


class FileTooLargeError(ToolError):
    """File exceeds maximum size for parsing."""


# --- Registration ---

_TOOLS: dict[str, object] = {}


def tool(fn=None, *, guidelines=()):
    """Decorator that registers an async function as a core tool.

    Every core tool is a native tool, a flat top-level name inside ``exec``,
    and is listed in the exec description. ``guidelines`` are added to the
    system prompt. Integrations are plugins and register with
    ``archie_agent.exec.plugin.tool`` instead (see docs/plugins.md).

    Usage:
        @tool(guidelines=("Use `read` to examine files.",))
        async def read(path, offset=None, limit=None, raw=False):
            ...

        # Also works without arguments (no guidelines):
        @tool
        async def some_tool():
            ...
    """

    def decorator(f):
        f._guidelines = tuple(guidelines)
        key = f.__name__
        if key in _TOOLS:
            raise ValueError(f"Duplicate exec tool registration: {key}")
        _TOOLS[key] = f
        return f

    if fn is not None:
        # Called as bare @tool (no parentheses)
        return decorator(fn)
    return decorator


def get_all_tools() -> dict:
    """Return all registered core tool functions.

    Returns a dict of name → async function. These become the native tool
    specs, the exec description and the system-prompt guidelines; runner.py
    injects them flat into the model code's namespace. Plugin tools are not
    included (D22).
    """
    # Import submodules to trigger @tool registration
    from archie_agent.exec.tools import (  # noqa: F401
        brain,
        code,
        fs,
        shell,
        web,
    )

    return dict(_TOOLS)


def get_tool_guidelines() -> list[str]:
    """Collect unique _guidelines from all registered exec tool functions.

    Iterates tools in sorted name order for deterministic output; a guideline
    shared by several tools appears once, at its first occurrence.
    Functions without a _guidelines attribute are silently skipped.

    Returns:
        Flat, de-duplicated list of guideline strings.
    """
    guidelines: list[str] = []
    seen: set[str] = set()
    for name in sorted(get_all_tools()):
        fn = _TOOLS[name]
        for g in getattr(fn, "_guidelines", ()):
            if g not in seen:
                seen.add(g)
                guidelines.append(g)
    return guidelines
