"""Native tool registration — generates ToolSpecs from @tool-decorated functions.

Produces JSON schemas from function signatures and wraps tool functions with
a truncation backstop for native (direct-call) dispatch. The same underlying
function is used by both native dispatch and code-mode (exec), ensuring a
single source of truth.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from functools import wraps
from typing import Any, Union, get_args, get_origin

from archie_agent.exec.tool import _MAX_RESULT_CHARS
from archie_agent.exec.tools import get_all_tools
from archie_agent.tools import ToolSpec

log = logging.getLogger(__name__)

# Type hint → JSON schema type mapping
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    bool: "boolean",
    float: "number",
}


def _schema_from_signature(fn: Callable) -> dict[str, Any]:
    """Generate a JSON schema from a function's type hints and docstring.

    Handles: str, int, bool, float, list, list[X], dict, Optional[X], X | None.
    Parameters with no default and not Optional are required.
    Descriptions come from the Google-style Args: docstring section.

    Uses typing.get_type_hints() to resolve stringified annotations from
    `from __future__ import annotations`.
    """
    import typing

    sig = inspect.signature(fn)
    arg_descriptions = _docstring_arg_descriptions(fn)

    # get_type_hints resolves string annotations to actual types
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        annotation = hints.get(name, param.annotation)
        is_optional = False

        if annotation is inspect.Parameter.empty:
            log.warning("Parameter %s.%s has no type annotation — defaulting to string", fn.__name__, name)
            json_type = "string"
        else:
            json_type, is_optional = _resolve_type(annotation)

        prop: dict[str, Any] = {"type": json_type}

        # Add description from docstring if available
        desc = arg_descriptions.get(name)
        if desc:
            prop["description"] = desc

        properties[name] = prop

        # Required if no default AND not Optional
        if param.default is inspect.Parameter.empty and not is_optional:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return schema


def _resolve_type(annotation) -> tuple[str, bool]:
    """Resolve a type annotation to a JSON schema type string.

    Returns (json_type, is_optional).
    """
    import types

    origin = get_origin(annotation)

    # Handle Union types (X | None, Optional[X]) — both typing.Union and types.UnionType
    if origin is Union or isinstance(annotation, types.UnionType):
        args = get_args(annotation)
        # Filter out NoneType
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            # Optional[X] or X | None
            inner = non_none[0]
            inner_origin = get_origin(inner)
            if inner_origin is list:
                return "array", True
            elif inner_origin is dict:
                return "object", True
            elif inner is list:
                return "array", True
            elif inner is dict:
                return "object", True
            else:
                return _TYPE_MAP.get(inner, "string"), True
        # Union of 3+ types — fall back to string
        log.warning("Complex union type %s — defaulting to string", annotation)
        return "string", False

    # Handle parameterized generics: list[X], dict[K, V]
    if origin is list:
        return "array", False
    if origin is dict:
        return "object", False

    # Handle bare types
    if annotation is list:
        return "array", False
    if annotation is dict:
        return "object", False

    json_type = _TYPE_MAP.get(annotation, "string")
    if json_type == "string" and annotation not in (str, inspect.Parameter.empty):
        log.warning("Unknown type annotation %s — defaulting to string", annotation)

    return json_type, False


def _docstring_arg_descriptions(fn: Callable) -> dict[str, str]:
    """Parse Google-style Args: section from a function's docstring.

    Lenient parser — extracts parameter descriptions from blocks like:
        Args:
            param_name: Description text that may
                continue on indented lines.
            another_param: Another description.

    Returns a dict of param_name → description string.
    """
    doc = inspect.getdoc(fn)
    if not doc:
        return {}

    # Find the Args: section
    args_match = re.search(r"^Args:\s*$", doc, re.MULTILINE)
    if not args_match:
        return {}

    # Extract the indented block after Args:
    lines = doc[args_match.end() :].split("\n")
    descriptions: dict[str, str] = {}
    current_param: str | None = None
    current_desc_parts: list[str] = []

    for line in lines:
        # End of Args section: next section header (Returns:, Raises:, etc.)
        # or a non-indented non-empty line
        if line and not line.startswith(" ") and not line.startswith("\t"):
            break

        stripped = line.strip()
        if not stripped:
            # Blank line — may end current param or be within a multi-line desc
            if current_param:
                # Treat as end of current param description
                descriptions[current_param] = " ".join(current_desc_parts).strip()
                current_param = None
                current_desc_parts = []
            continue

        # Check if this is a new parameter line: "param_name: description"
        # or "param_name (type): description"
        param_match = re.match(r"^(\w+)(?:\s*\([^)]*\))?\s*:\s*(.*)$", stripped)
        if param_match:
            # Save previous param
            if current_param:
                descriptions[current_param] = " ".join(current_desc_parts).strip()

            current_param = param_match.group(1)
            desc_start = param_match.group(2).strip()
            current_desc_parts = [desc_start] if desc_start else []
        elif current_param:
            # Continuation line for current parameter
            current_desc_parts.append(stripped)

    # Flush last parameter
    if current_param:
        descriptions[current_param] = " ".join(current_desc_parts).strip()

    return descriptions


def _tool_description(fn: Callable) -> str:
    """Extract the tool description from the function's docstring.

    Uses the first paragraph (up to the first blank line or Args: section).
    """
    doc = inspect.getdoc(fn)
    if not doc:
        return fn.__name__

    lines: list[str] = []
    for line in doc.split("\n"):
        stripped = line.strip()
        if not stripped:
            break
        if stripped.startswith("Args:"):
            break
        lines.append(stripped)

    return " ".join(lines) if lines else fn.__name__


def _native_handler(fn: Callable) -> Callable:
    """Wrap a tool function with the global truncation backstop.

    The wrapper awaits the tool function and applies _MAX_RESULT_CHARS
    truncation. Since all tool fns now return strings, this is a simple
    passthrough + length check.

    The harness's generic dispatch does str(result) on the return — since
    the wrapper already returns a string, str() is a no-op.
    """

    @wraps(fn)
    async def wrapper(**kwargs) -> str:
        result = await fn(**kwargs)
        # Defensive: all tools should return str, but handle None gracefully
        if result is None:
            return ""
        text = str(result)
        if len(text) > _MAX_RESULT_CHARS:
            return text[:_MAX_RESULT_CHARS] + "\n[…truncated]"
        return text

    return wrapper


def make_native_specs() -> list[ToolSpec]:
    """Generate ToolSpecs for all tools marked as native.

    Each spec has:
    - name: the function name
    - description: first paragraph of docstring
    - schema: JSON schema generated from signature + docstring
    - handler: the function wrapped with truncation backstop
    """
    specs: list[ToolSpec] = []
    tools = get_all_tools()

    for name in sorted(tools):
        fn = tools[name]
        if not getattr(fn, "_native", True):
            continue

        specs.append(
            ToolSpec(
                name=name,
                description=_tool_description(fn),
                schema=_schema_from_signature(fn),
                handler=_native_handler(fn),
            )
        )

    return specs
