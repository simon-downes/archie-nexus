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


def tool(
    fn=None,
    *,
    guidelines=(),
    native: bool = True,
    exec_enabled: bool = True,
    exec_docs: bool = True,
    namespace: str | None = None,
):
    """Decorator that registers an async function as an exec tool.

    Usage:
        @tool(guidelines=("Use `read` to examine files.",))
        async def read(path, offset=None, limit=None, raw=False):
            ...

        # Also works without arguments (no guidelines):
        @tool
        async def some_tool():
            ...

        # Mark a tool as code-mode-only (excluded from native registration):
        @tool(native=False)
        async def internal_tool():
            ...
    """

    def decorator(f):
        f._guidelines = guidelines or (
            ("Use Jira only through the `jira` namespace and never provide credentials.",)
            if namespace
            else ()
        )
        f._native = native
        f._exec_enabled = exec_enabled
        f._exec_docs = exec_docs
        f._namespace = namespace
        key = f"{namespace}.{f.__name__}" if namespace else f.__name__
        if key in _TOOLS:
            raise ValueError(f"Duplicate exec tool registration: {key}")
        _TOOLS[key] = f
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
    from archie_agent.exec.tools import (  # noqa: F401
        brain,
        calendar,
        code,
        discovery,
        drive,
        fs,
        jira,
        mail,
        meet,
        notion,
        shell,
        slack,
        web,
    )

    return {key: value for key, value in _TOOLS.items() if getattr(value, "_exec_enabled", True)}


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
