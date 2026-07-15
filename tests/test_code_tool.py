"""Tests for the code exec tool — tree-sitter structural code intelligence."""

import pytest
from archie_agent.exec.tools import (
    BinaryFileError,
    FileNotFoundError,
    FileTooLargeError,
    PathValidationError,
    UnsupportedLanguageError,
    get_all_tools,
    get_tool_guidelines,
)
from archie_agent.exec.tools.code import (
    WORKSPACE,
    Symbol,
    _discover_files_fallback,
    _extract_css,
    _extract_go,
    _extract_hcl,
    _extract_javascript,
    _extract_php,
    _extract_python,
    _extract_rust,
    _filter_symbols,
    _parse_file,
    _resolve_path,
    code,
)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """Tests for tool registration."""

    def test_code_registered(self):
        tools = get_all_tools()
        assert "code" in tools

    def test_code_has_guidelines(self):
        tools = get_all_tools()
        assert hasattr(tools["code"], "_guidelines")
        assert len(tools["code"]._guidelines) > 0

    def test_guidelines_in_aggregation(self):
        guidelines = get_tool_guidelines()
        assert any("code" in g for g in guidelines)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    """Tests for _resolve_path."""

    def test_empty_returns_workspace(self):
        assert _resolve_path("") == WORKSPACE

    def test_relative_resolves_under_workspace(self):
        result = _resolve_path("src/main.py")
        assert str(result).startswith(str(WORKSPACE))

    def test_absolute_under_workspace_ok(self):
        result = _resolve_path(str(WORKSPACE / "src"))
        assert result == (WORKSPACE / "src").resolve()

    def test_absolute_outside_workspace_raises(self):
        with pytest.raises(PathValidationError):
            _resolve_path("/etc/passwd")

    def test_traversal_outside_raises(self):
        with pytest.raises(PathValidationError):
            _resolve_path("../../etc/passwd")


# ---------------------------------------------------------------------------
# File parsing infrastructure
# ---------------------------------------------------------------------------


class TestParseFile:
    """Tests for _parse_file validation."""

    def test_unsupported_extension(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "test.xyz"
        f.write_text("content")
        with pytest.raises(UnsupportedLanguageError):
            _parse_file(f)

    def test_file_too_large(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "big.py"
        f.write_bytes(b"x" * 600_000)
        with pytest.raises(FileTooLargeError):
            _parse_file(f)

    def test_binary_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "binary.py"
        f.write_bytes(b"\x00\x01\x02" + b"def main(): pass")
        with pytest.raises(BinaryFileError):
            _parse_file(f)

    def test_nonexistent_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "nope.py"
        with pytest.raises(FileNotFoundError):
            _parse_file(f)

    def test_language_filter_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "test.py"
        f.write_text("def hello(): pass")
        result = _parse_file(f, language="javascript")
        assert result == []


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    """Tests for file discovery fallback."""

    def test_fallback_finds_python_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.txt").write_text("not source")
        files = _discover_files_fallback(tmp_path)
        assert len(files) == 1
        assert files[0].suffix == ".py"

    def test_fallback_skips_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "pkg.js").write_text("module.exports = {}")
        (tmp_path / "app.js").write_text("function main() {}")
        files = _discover_files_fallback(tmp_path)
        assert all("node_modules" not in str(f) for f in files)

    def test_fallback_language_filter(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.js").write_text("const x = 1")
        files = _discover_files_fallback(tmp_path, language="python")
        assert len(files) == 1
        assert files[0].suffix == ".py"

    def test_fallback_skips_large_files(self, tmp_path):
        (tmp_path / "big.py").write_bytes(b"x" * 600_000)
        (tmp_path / "small.py").write_text("x = 1")
        files = _discover_files_fallback(tmp_path)
        assert len(files) == 1
        assert "small" in files[0].name


# ---------------------------------------------------------------------------
# Symbol filtering
# ---------------------------------------------------------------------------


class TestFilterSymbols:
    """Tests for _filter_symbols."""

    def test_matches_name(self):
        syms = [Symbol("hello", "function", 1, 1, "def hello()")]
        result = _filter_symbols(syms, "hello")
        assert len(result) == 1

    def test_case_insensitive(self):
        syms = [Symbol("MyClass", "class", 1, 5, "class MyClass")]
        result = _filter_symbols(syms, "myclass")
        assert len(result) == 1

    def test_substring_match(self):
        syms = [Symbol("handle_request", "function", 1, 10, "def handle_request()")]
        result = _filter_symbols(syms, "request")
        assert len(result) == 1

    def test_searches_children(self):
        child = Symbol("inner", "method", 3, 5, "def inner()")
        parent = Symbol("Outer", "class", 1, 10, "class Outer", children=[child])
        result = _filter_symbols([parent], "inner")
        assert len(result) == 1
        assert result[0].name == "inner"

    def test_no_match_returns_empty(self):
        syms = [Symbol("foo", "function", 1, 1, "def foo()")]
        result = _filter_symbols(syms, "zzz")
        assert result == []


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------


class TestExtractPython:
    """Tests for Python symbol extraction."""

    def _parse(self, source: str) -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser("python")
        tree = parser.parse(source.encode())
        return _extract_python(tree.root_node, source.encode())

    def test_function(self):
        syms = self._parse("def hello(x: int) -> str:\n    return str(x)\n")
        assert len(syms) == 1
        assert syms[0].name == "hello"
        assert syms[0].kind == "function"
        assert "def hello" in syms[0].signature

    def test_class_with_methods(self):
        src = "class Foo:\n    def bar(self):\n        pass\n    def baz(self):\n        pass\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].name == "Foo"
        assert syms[0].kind == "class"
        assert len(syms[0].children) == 2
        assert syms[0].children[0].kind == "method"

    def test_decorated_function(self):
        src = "@app.route('/hello')\ndef hello():\n    pass\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].name == "hello"
        assert "@" in syms[0].signature

    def test_constant(self):
        src = "MAX_SIZE = 100\nlocal_var = 'not a constant'\n"
        syms = self._parse(src)
        assert any(s.name == "MAX_SIZE" and s.kind == "constant" for s in syms)
        assert not any(s.name == "local_var" for s in syms)

    def test_empty_file(self):
        syms = self._parse("")
        assert syms == []


# ---------------------------------------------------------------------------
# JavaScript extractor
# ---------------------------------------------------------------------------


class TestExtractJavaScript:
    """Tests for JavaScript/TypeScript symbol extraction."""

    def _parse(self, source: str, lang: str = "javascript") -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser(lang)
        tree = parser.parse(source.encode())
        return _extract_javascript(tree.root_node, source.encode())

    def test_function_declaration(self):
        syms = self._parse("function hello(x) { return x; }\n")
        assert len(syms) == 1
        assert syms[0].name == "hello"
        assert syms[0].kind == "function"

    def test_class_with_methods(self):
        src = "class Foo {\n  bar() {}\n  baz() {}\n}\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].name == "Foo"
        assert len(syms[0].children) == 2

    def test_exported_arrow_function(self):
        src = "export const handler = (event) => { return event; }\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].name == "handler"

    def test_typescript(self):
        src = "function greet(name: string): string { return name; }\n"
        syms = self._parse(src, lang="typescript")
        assert len(syms) == 1
        assert syms[0].name == "greet"


# ---------------------------------------------------------------------------
# Go extractor
# ---------------------------------------------------------------------------


class TestExtractGo:
    """Tests for Go symbol extraction."""

    def _parse(self, source: str) -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser("go")
        tree = parser.parse(source.encode())
        return _extract_go(tree.root_node, source.encode())

    def test_function(self):
        src = "package main\n\nfunc Hello(name string) string {\n\treturn name\n}\n"
        syms = self._parse(src)
        assert any(s.name == "Hello" and s.kind == "function" for s in syms)

    def test_method(self):
        src = "package main\n\nfunc (s *Server) Start() error {\n\treturn nil\n}\n"
        syms = self._parse(src)
        assert any(s.name == "Start" and s.kind == "method" for s in syms)

    def test_type(self):
        src = "package main\n\ntype Server struct {\n\tPort int\n}\n"
        syms = self._parse(src)
        assert any(s.name == "Server" and s.kind == "type" for s in syms)

    def test_interface(self):
        src = "package main\n\ntype Handler interface {\n\tHandle() error\n}\n"
        syms = self._parse(src)
        assert any(s.name == "Handler" and s.kind == "interface" for s in syms)


# ---------------------------------------------------------------------------
# Rust extractor
# ---------------------------------------------------------------------------


class TestExtractRust:
    """Tests for Rust symbol extraction."""

    def _parse(self, source: str) -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser("rust")
        tree = parser.parse(source.encode())
        return _extract_rust(tree.root_node, source.encode())

    def test_function(self):
        src = "fn hello(name: &str) -> String {\n    name.to_string()\n}\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].name == "hello"
        assert syms[0].kind == "function"

    def test_struct(self):
        src = "struct Point {\n    x: f64,\n    y: f64,\n}\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].name == "Point"
        assert syms[0].kind == "struct"

    def test_impl_with_methods(self):
        src = "impl Point {\n    fn new(x: f64) -> Self {\n        Self { x, y: 0.0 }\n    }\n}\n"
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].kind == "impl"
        assert len(syms[0].children) == 1
        assert syms[0].children[0].kind == "method"


# ---------------------------------------------------------------------------
# PHP extractor
# ---------------------------------------------------------------------------


class TestExtractPHP:
    """Tests for PHP symbol extraction."""

    def _parse(self, source: str) -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser("php")
        tree = parser.parse(source.encode())
        return _extract_php(tree.root_node, source.encode())

    def test_function(self):
        src = "<?php\nfunction hello($name) {\n    return $name;\n}\n"
        syms = self._parse(src)
        assert any(s.name == "hello" and s.kind == "function" for s in syms)

    def test_class_with_methods(self):
        src = "<?php\nclass Foo {\n    public function bar() {}\n}\n"
        syms = self._parse(src)
        classes = [s for s in syms if s.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "Foo"
        assert len(classes[0].children) >= 1


# ---------------------------------------------------------------------------
# CSS extractor
# ---------------------------------------------------------------------------


class TestExtractCSS:
    """Tests for CSS symbol extraction."""

    def _parse(self, source: str) -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser("css")
        tree = parser.parse(source.encode())
        return _extract_css(tree.root_node, source.encode())

    def test_selectors(self):
        src = ".header { color: red; }\n#main { display: flex; }\n"
        syms = self._parse(src)
        assert len(syms) == 2
        assert all(s.kind == "selector" for s in syms)
        names = {s.name for s in syms}
        assert ".header" in names
        assert "#main" in names


# ---------------------------------------------------------------------------
# HCL extractor
# ---------------------------------------------------------------------------


class TestExtractHCL:
    """Tests for HCL/Terraform symbol extraction."""

    def _parse(self, source: str) -> list[Symbol]:
        from archie_agent.exec.tools.code import _get_parser

        parser = _get_parser("hcl")
        tree = parser.parse(source.encode())
        return _extract_hcl(tree.root_node, source.encode())

    def test_resource_block(self):
        src = 'resource "aws_instance" "web" {\n  ami = "abc"\n}\n'
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].kind == "resource"

    def test_variable_block(self):
        src = 'variable "name" {\n  type = string\n}\n'
        syms = self._parse(src)
        assert len(syms) == 1
        assert syms[0].kind == "variable"


# ---------------------------------------------------------------------------
# Integration: full code() function
# ---------------------------------------------------------------------------


class TestCodeIntegration:
    """Integration tests for the code() tool function."""

    @pytest.mark.asyncio
    async def test_file_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "app.py"
        f.write_text("def main():\n    pass\n\ndef helper():\n    pass\n")
        result = await code(path="app.py")
        assert "app.py (python," in result
        assert "def main()" in result
        assert "def helper()" in result

    @pytest.mark.asyncio
    async def test_directory_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        (tmp_path / "a.py").write_text("def foo(): pass\n")
        (tmp_path / "b.py").write_text("def bar(): pass\n")
        result = await code(path=".")
        assert "a.py" in result
        assert "b.py" in result
        assert "foo" in result
        assert "bar" in result

    @pytest.mark.asyncio
    async def test_name_search(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        (tmp_path / "a.py").write_text("def foo(): pass\ndef bar(): pass\n")
        result = await code(path="a.py", name="foo")
        assert "foo" in result
        assert "bar" not in result

    @pytest.mark.asyncio
    async def test_language_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        (tmp_path / "a.py").write_text("def foo(): pass\n")
        (tmp_path / "b.js").write_text("function bar() {}\n")
        result = await code(path=".", language="python")
        assert "foo" in result
        assert "bar" not in result

    @pytest.mark.asyncio
    async def test_nonexistent_path_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        with pytest.raises(FileNotFoundError):
            await code(path="nonexistent.py")

    @pytest.mark.asyncio
    async def test_directory_skips_erroring_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        (tmp_path / "good.py").write_text("def foo(): pass\n")
        (tmp_path / "bad.py").write_bytes(b"\x00binary content")
        result = await code(path=".")
        assert "foo" in result

    @pytest.mark.asyncio
    async def test_symbol_dict_structure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("archie_agent.exec.tools.code.WORKSPACE", tmp_path)
        f = tmp_path / "test.py"
        f.write_text("class Foo:\n    def bar(self):\n        pass\n")
        result = await code(path="test.py")
        assert "Foo" in result
        assert "bar" in result
        assert "class" in result
        assert "method" in result
