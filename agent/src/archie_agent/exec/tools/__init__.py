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


# --- Registration ---

_TOOLS: dict[str, object] = {}


def tool(fn=None, *, guidelines=()):
    """Decorator that registers an async function as an exec tool.

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
        f._guidelines = guidelines
        _TOOLS[f.__name__] = f
        return f

    if fn is not None:
        # Called as bare @tool (no parentheses)
        return decorator(fn)
    return decorator


def get_all_tools() -> dict:
    """Return all registered exec tool functions.

    Returns a dict of name → async function. runner.py injects these
    into the model code's namespace.
    """
    # Import submodules to trigger @tool registration
    from archie_agent.exec.tools import fs, shell, web  # noqa: F401

    return dict(_TOOLS)


def get_tool_guidelines() -> list[str]:
    """Collect _guidelines from all registered exec tool functions.

    Iterates tools in sorted name order for deterministic output.
    Functions without a _guidelines attribute are silently skipped.

    Returns:
        Flat list of guideline strings aggregated from all tools.
    """
    guidelines: list[str] = []
    for name in sorted(get_all_tools()):
        fn = _TOOLS[name]
        for g in getattr(fn, "_guidelines", ()):
            guidelines.append(g)
    return guidelines
