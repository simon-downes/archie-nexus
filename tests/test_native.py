"""Tests for native tool spec generation — schema, descriptions, and handler wrapper."""

import pytest
from archie_agent.exec.native import (
    _docstring_arg_descriptions,
    _native_handler,
    _schema_from_signature,
    _tool_description,
    make_native_specs,
)
from archie_agent.exec.tool import _MAX_RESULT_CHARS

# ---------------------------------------------------------------------------
# _schema_from_signature tests
# ---------------------------------------------------------------------------


async def _sample_fn(
    path: str,
    offset: int | None = None,
    limit: int | None = None,
    raw: bool = False,
) -> str:
    """Sample function for testing.

    Args:
        path: File path to read.
        offset: Starting line number.
        limit: Maximum lines to return.
        raw: If True, return raw content.
    """
    return ""


async def _no_defaults_fn(pattern: str, include: str | None = None) -> str:
    """Search for pattern.

    Args:
        pattern: Regex pattern to search for.
        include: File glob filter.
    """
    return ""


async def _list_param_fn(items: list[str], count: int = 10) -> str:
    """Process items.

    Args:
        items: List of item strings.
        count: Maximum items to process.
    """
    return ""


class TestSchemaFromSignature:
    def test_required_string_param(self):
        schema = _schema_from_signature(_sample_fn)
        assert schema["properties"]["path"]["type"] == "string"
        assert "path" in schema["required"]

    def test_optional_int_param(self):
        schema = _schema_from_signature(_sample_fn)
        assert schema["properties"]["offset"]["type"] == "integer"
        assert "offset" not in schema["required"]

    def test_bool_with_default(self):
        schema = _schema_from_signature(_sample_fn)
        assert schema["properties"]["raw"]["type"] == "boolean"
        assert "raw" not in schema["required"]

    def test_required_list_excludes_optional(self):
        schema = _schema_from_signature(_no_defaults_fn)
        assert "pattern" in schema["required"]
        assert "include" not in schema["required"]

    def test_optional_union_type(self):
        schema = _schema_from_signature(_no_defaults_fn)
        # include: str | None → type is string, not required
        assert schema["properties"]["include"]["type"] == "string"

    def test_list_type(self):
        schema = _schema_from_signature(_list_param_fn)
        assert schema["properties"]["items"]["type"] == "array"
        assert "items" in schema["required"]

    def test_descriptions_from_docstring(self):
        schema = _schema_from_signature(_sample_fn)
        assert schema["properties"]["path"]["description"] == "File path to read."
        assert schema["properties"]["offset"]["description"] == "Starting line number."
        assert schema["properties"]["raw"]["description"] == "If True, return raw content."


# ---------------------------------------------------------------------------
# _docstring_arg_descriptions tests
# ---------------------------------------------------------------------------


class TestDocstringArgDescriptions:
    def test_basic_extraction(self):
        descs = _docstring_arg_descriptions(_sample_fn)
        assert descs["path"] == "File path to read."
        assert descs["raw"] == "If True, return raw content."

    def test_multiline_description(self):
        async def fn(x: str) -> str:
            """A function.

            Args:
                x: This is a long description
                    that continues on the next line
                    and even further.
            """
            return ""

        descs = _docstring_arg_descriptions(fn)
        assert "long description" in descs["x"]
        assert "continues on the next line" in descs["x"]
        assert "even further" in descs["x"]

    def test_stops_at_returns_section(self):
        async def fn(a: str) -> str:
            """Doc.

            Args:
                a: First param.

            Returns:
                Some value.
            """
            return ""

        descs = _docstring_arg_descriptions(fn)
        assert descs["a"] == "First param."
        assert "Some value" not in str(descs)

    def test_no_args_section(self):
        async def fn(x: str) -> str:
            """A function without Args section."""
            return ""

        descs = _docstring_arg_descriptions(fn)
        assert descs == {}

    def test_no_docstring(self):
        async def fn(x: str) -> str:
            pass

        descs = _docstring_arg_descriptions(fn)
        assert descs == {}


# ---------------------------------------------------------------------------
# _tool_description tests
# ---------------------------------------------------------------------------


class TestToolDescription:
    def test_single_line(self):
        async def fn() -> str:
            """Do something useful."""
            return ""

        assert _tool_description(fn) == "Do something useful."

    def test_multiline_first_paragraph(self):
        async def fn() -> str:
            """Do something useful that requires
            a longer explanation.

            Args:
                nothing: here.
            """
            return ""

        desc = _tool_description(fn)
        assert "Do something useful" in desc
        assert "longer explanation" in desc
        assert "nothing" not in desc

    def test_stops_at_blank_line(self):
        async def fn() -> str:
            """First paragraph.

            Second paragraph not included.
            """
            return ""

        assert _tool_description(fn) == "First paragraph."


# ---------------------------------------------------------------------------
# _native_handler tests
# ---------------------------------------------------------------------------


class TestNativeHandler:
    @pytest.mark.asyncio
    async def test_passthrough_string(self):
        async def fn(**kwargs) -> str:
            return "hello world"

        handler = _native_handler(fn)
        result = await handler()
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_none_returns_empty(self):
        async def fn(**kwargs) -> None:
            return None

        handler = _native_handler(fn)
        result = await handler()
        assert result == ""

    @pytest.mark.asyncio
    async def test_truncation_at_max_chars(self):
        long_text = "x" * (_MAX_RESULT_CHARS + 1000)

        async def fn(**kwargs) -> str:
            return long_text

        handler = _native_handler(fn)
        result = await handler()
        assert len(result) < len(long_text)
        assert result.endswith("\n[…truncated]")
        assert len(result) == _MAX_RESULT_CHARS + len("\n[…truncated]")

    @pytest.mark.asyncio
    async def test_no_truncation_under_limit(self):
        text = "x" * 100

        async def fn(**kwargs) -> str:
            return text

        handler = _native_handler(fn)
        result = await handler()
        assert result == text
        assert "[…truncated]" not in result


# ---------------------------------------------------------------------------
# make_native_specs tests
# ---------------------------------------------------------------------------


class TestMakeNativeSpecs:
    def test_produces_specs_for_all_native_tools(self):
        specs = make_native_specs()
        names = {s.name for s in specs}
        # All native tools should be present
        assert "read" in names
        assert "grep" in names
        assert "glob" in names
        assert "edit" in names
        assert "write" in names
        assert "shell" in names
        assert "web_fetch" in names
        assert "web_search" in names
        assert "code" in names
        assert "brain_search" in names
        brain = next(spec for spec in specs if spec.name == "brain_search")
        assert set(brain.schema["properties"]) == {"query", "limit"}
        assert "regular filesystem" in brain.description

    def test_spec_has_required_fields(self):
        specs = make_native_specs()
        for spec in specs:
            assert spec.name
            assert spec.description
            assert spec.schema
            assert spec.handler is not None
            assert "type" in spec.schema
            assert "properties" in spec.schema

    def test_native_false_excluded(self):
        """Tools marked native=False are not included."""
        from archie_agent.exec.tools import _TOOLS, tool

        # Register a test-only tool with native=False
        @tool(native=False)
        async def _test_only_internal() -> str:
            """Internal tool."""
            return ""

        try:
            specs = make_native_specs()
            names = {s.name for s in specs}
            assert "_test_only_internal" not in names
        finally:
            # Clean up
            del _TOOLS["_test_only_internal"]

    def test_exec_not_in_native_specs(self):
        """exec is not a native spec — it's registered separately."""
        specs = make_native_specs()
        names = {s.name for s in specs}
        assert "exec" not in names
