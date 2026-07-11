"""Code intelligence tool — tree-sitter based structural code understanding.

Extracts symbol definitions (functions, classes, methods, types) from source files.
Supports 9 languages with lazy parser loading and ripgrep-based file discovery.

Imports are deferred (tree-sitter loaded on first use per language).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from archie_agent.exec.tools import (
    BinaryFileError,
    FileNotFoundError,
    FileTooLargeError,
    PathValidationError,
    UnsupportedLanguageError,
    tool,
)
from archie_agent.exec.tools.fs import WORKSPACE

# Maximum file size to parse (skip generated/minified files)
_MAX_FILE_SIZE = 500_000  # 500KB

# Directories to always skip (fallback path only; rg handles via .gitignore)
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".git", ".tox"}

# Maximum symbols returned in search mode
_MAX_RESULTS = 50


# ---------------------------------------------------------------------------
# Symbol dataclass
# ---------------------------------------------------------------------------


@dataclass
class Symbol:
    """A code symbol (function, class, method, etc.)."""

    name: str
    kind: str
    line: int  # 1-based
    end_line: int  # 1-based, inclusive
    signature: str
    children: list[Symbol] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dict for tool return value."""
        d: dict = {
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "end_line": self.end_line,
            "signature": self.signature,
        }
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


# ---------------------------------------------------------------------------
# Language loaders (lazy — import grammar on first use)
# ---------------------------------------------------------------------------

_parsers: dict[str, object] = {}


def _get_parser(lang_name: str):
    """Get or create a tree-sitter parser for the given language."""
    import tree_sitter

    if lang_name in _parsers:
        return _parsers[lang_name]

    loader = _LANGUAGE_LOADERS.get(lang_name)
    if not loader:
        raise UnsupportedLanguageError(
            f"Unsupported language: {lang_name}. "
            f"Supported: {', '.join(sorted(_LANGUAGE_LOADERS.keys()))}"
        )

    try:
        language = loader()
        parser = tree_sitter.Parser(language)
        _parsers[lang_name] = parser
        return parser
    except (ImportError, OSError) as e:
        raise UnsupportedLanguageError(f"Failed to load grammar for {lang_name}: {e}") from e


def _load_python():
    import tree_sitter
    import tree_sitter_python

    return tree_sitter.Language(tree_sitter_python.language())


def _load_javascript():
    import tree_sitter
    import tree_sitter_javascript

    return tree_sitter.Language(tree_sitter_javascript.language())


def _load_typescript():
    import tree_sitter
    import tree_sitter_typescript

    return tree_sitter.Language(tree_sitter_typescript.language_typescript())


def _load_tsx():
    import tree_sitter
    import tree_sitter_typescript

    return tree_sitter.Language(tree_sitter_typescript.language_tsx())


def _load_php():
    import tree_sitter
    import tree_sitter_php

    return tree_sitter.Language(tree_sitter_php.language_php())


def _load_go():
    import tree_sitter
    import tree_sitter_go

    return tree_sitter.Language(tree_sitter_go.language())


def _load_rust():
    import tree_sitter
    import tree_sitter_rust

    return tree_sitter.Language(tree_sitter_rust.language())


def _load_css():
    import tree_sitter
    import tree_sitter_css

    return tree_sitter.Language(tree_sitter_css.language())


def _load_hcl():
    import tree_sitter
    import tree_sitter_hcl

    return tree_sitter.Language(tree_sitter_hcl.language())


_LANGUAGE_LOADERS: dict[str, Callable] = {
    "python": _load_python,
    "javascript": _load_javascript,
    "typescript": _load_typescript,
    "tsx": _load_tsx,
    "php": _load_php,
    "go": _load_go,
    "rust": _load_rust,
    "css": _load_css,
    "hcl": _load_hcl,
}

# Extension → language name
_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
    ".go": "go",
    ".rs": "rust",
    ".css": "css",
    ".scss": "css",
    ".tf": "hcl",
    ".hcl": "hcl",
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_path(path: str) -> Path:
    """Resolve path relative to WORKSPACE and validate."""
    if not path:
        return WORKSPACE

    p = Path(path)
    if p.is_absolute():
        if not (str(p) == str(WORKSPACE) or str(p).startswith(str(WORKSPACE) + "/")):
            raise PathValidationError(f"Absolute path '{path}' is not under {WORKSPACE}.")
        resolved = p.resolve()
    else:
        resolved = (WORKSPACE / p).resolve()

    try:
        resolved.relative_to(WORKSPACE.resolve())
    except ValueError:
        raise PathValidationError(f"Path '{path}' resolves outside {WORKSPACE}.") from None

    return resolved


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


async def _discover_files(dir_path: Path, language: str | None = None) -> list[Path]:
    """Discover source files using ripgrep (respects .gitignore)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rg",
            "--files",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(dir_path),
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return _discover_files_fallback(dir_path, language)

    if proc.returncode not in (0, 1):
        return _discover_files_fallback(dir_path, language)

    files: list[Path] = []
    for line in stdout.decode("utf-8", errors="replace").strip().split("\n"):
        if not line:
            continue
        p = (dir_path / line).resolve()
        if p.suffix not in _EXTENSION_MAP:
            continue
        if language and _EXTENSION_MAP[p.suffix] != language:
            continue
        try:
            if p.stat().st_size > _MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        files.append(p)

    return files


def _discover_files_fallback(dir_path: Path, language: str | None = None) -> list[Path]:
    """Fallback file discovery without ripgrep."""
    files: list[Path] = []
    try:
        for p in dir_path.rglob("*"):
            if any(skip in p.parts for skip in _SKIP_DIRS):
                continue
            if not p.is_file():
                continue
            if p.suffix not in _EXTENSION_MAP:
                continue
            if language and _EXTENSION_MAP[p.suffix] != language:
                continue
            try:
                if p.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files.append(p)
    except OSError:
        pass
    return files


# ---------------------------------------------------------------------------
# Filtering and search
# ---------------------------------------------------------------------------


def _filter_symbols(symbols: list[Symbol], name: str) -> list[Symbol]:
    """Filter symbols by name (case-insensitive substring), including children."""
    name_lower = name.lower()
    results: list[Symbol] = []
    for sym in symbols:
        if name_lower in sym.name.lower():
            results.append(sym)
        child_matches = _filter_symbols(sym.children, name)
        results.extend(child_matches)
    return results


def _search_symbols(files: list[Path], name: str) -> list[dict]:
    """Search for symbols by name across multiple files."""
    results: list[dict] = []

    for f in files:
        try:
            symbols = _parse_file(f)
        except Exception:
            continue

        matches = _filter_symbols(symbols, name)
        if matches:
            rel = str(f.relative_to(WORKSPACE))
            for s in matches:
                d = s.to_dict()
                d["file"] = rel
                results.append(d)

        if len(results) >= _MAX_RESULTS:
            break

    return results[:_MAX_RESULTS]


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------


def _parse_file(file_path: Path, language: str | None = None) -> list[Symbol]:
    """Parse a single file and return its symbols."""
    ext = file_path.suffix
    lang_name = _EXTENSION_MAP.get(ext)

    if not lang_name:
        raise UnsupportedLanguageError(
            f"Unsupported file extension: {ext}. "
            f"Supported: {', '.join(sorted(_EXTENSION_MAP.keys()))}"
        )

    if language and lang_name != language:
        return []

    # Size check
    try:
        size = file_path.stat().st_size
    except OSError as e:
        raise FileNotFoundError(f"Cannot stat file: {e}") from e

    if size > _MAX_FILE_SIZE:
        raise FileTooLargeError(
            f"File too large ({size} bytes, max {_MAX_FILE_SIZE}): {file_path.name}"
        )

    # Read content
    try:
        content = file_path.read_bytes()
    except OSError as e:
        raise FileNotFoundError(f"Cannot read file: {e}") from e

    # Binary check
    if b"\x00" in content[:8192]:
        raise BinaryFileError(f"Binary file detected: {file_path.name}")

    # Parse with tree-sitter
    parser = _get_parser(lang_name)
    tree = parser.parse(content)

    # Extract symbols
    extractor = _EXTRACTORS.get(lang_name, _extract_generic)
    return extractor(tree.root_node, content)


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------


@tool(guidelines=("Use `code` to explore symbol structure before reading full files.",))
async def code(
    path: str | None = None,
    name: str | None = None,
    language: str | None = None,
) -> list[dict]:
    """Structural code intelligence — extract symbol definitions from source files.

    Args:
        path: File or directory (relative to /workspace/, default: /workspace/).
        name: Filter symbols by name (case-insensitive substring match).
        language: Filter by language (python, typescript, javascript, tsx, php, go, rust, css, hcl).

    Returns:
        List of symbol dicts with keys: name, kind, line, end_line, signature,
        children (optional), file (optional, in directory/search modes).
    """
    resolved = _resolve_path(path or "")

    if not resolved.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    if resolved.is_file():
        symbols = _parse_file(resolved, language)
        if name:
            symbols = _filter_symbols(symbols, name)
        return [s.to_dict() for s in symbols]
    else:
        # Directory mode
        files = await _discover_files(resolved, language)
        if name:
            return _search_symbols(files, name)
        # Return all symbols from all files
        all_symbols: list[dict] = []
        for f in files:
            try:
                syms = _parse_file(f, language)
                if syms:
                    rel = str(f.relative_to(WORKSPACE))
                    for s in syms:
                        d = s.to_dict()
                        d["file"] = rel
                        all_symbols.append(d)
            except Exception:
                continue
        return all_symbols


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _text(node, source: bytes) -> str:
    """Get the text of a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _extract_generic(root, source: bytes) -> list[Symbol]:
    """Fallback extractor — returns empty."""
    return []


# Extractor registry — populated after extractor functions are defined
_EXTRACTORS: dict[str, Callable] = {}


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------


def _extract_python(root, source: bytes) -> list[Symbol]:
    """Extract symbols from Python AST."""
    symbols = []
    for node in root.children:
        if node.type == "function_definition":
            symbols.append(_python_function(node, source))
        elif node.type == "class_definition":
            symbols.append(_python_class(node, source))
        elif node.type == "decorated_definition":
            decorators = []
            func_or_class = None
            for child in node.children:
                if child.type == "decorator":
                    dec_name = _extract_decorator_name(child, source)
                    decorators.append(dec_name)
                elif child.type in ("function_definition", "class_definition"):
                    func_or_class = child
            if func_or_class:
                if func_or_class.type == "function_definition":
                    symbols.append(_python_function(func_or_class, source, decorators))
                else:
                    symbols.append(_python_class(func_or_class, source, decorators))
        elif node.type == "expression_statement":
            for child in node.children:
                if child.type == "assignment":
                    sym = _python_constant(child, source)
                    if sym:
                        symbols.append(sym)
    return symbols


def _python_function(node, source: bytes, decorators: list[str] | None = None) -> Symbol:
    """Build a Symbol for a Python function_definition node."""
    name = _text(node.child_by_field_name("name"), source)
    params = _text(node.child_by_field_name("parameters"), source)
    ret_node = node.child_by_field_name("return_type")
    ret = f" -> {_text(ret_node, source)}" if ret_node else ""

    signature = f"def {name}{params}{ret}"
    if decorators:
        decorator_str = "\n".join(f"@{d}" for d in decorators)
        signature = f"{decorator_str}\n{signature}"

    return Symbol(
        name=name,
        kind="function",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=signature,
    )


def _extract_decorator_name(decorator_node, source: bytes) -> str:
    """Extract decorator name from a decorator node."""
    for child in decorator_node.children:
        if child.type == "identifier":
            return _text(child, source)
        elif child.type == "attribute":
            return _text(child, source)
        elif child.type == "call_expression":
            for sub in child.children:
                if sub.type in ("identifier", "attribute"):
                    return _text(sub, source)
    return "?"


def _python_class(node, source: bytes, decorators: list[str] | None = None) -> Symbol:
    """Build a Symbol for a Python class_definition node."""
    name = _text(node.child_by_field_name("name"), source)

    signature = f"class {name}"
    if decorators:
        decorator_str = "\n".join(f"@{d}" for d in decorators)
        signature = f"{decorator_str}\n{signature}"

    body = node.child_by_field_name("body")
    children = []
    if body:
        for child in body.children:
            if child.type == "function_definition":
                sym = _python_function(child, source)
                sym.kind = "method"
                children.append(sym)
            elif child.type == "decorated_definition":
                for sub in child.children:
                    if sub.type == "function_definition":
                        sym = _python_function(sub, source)
                        sym.kind = "method"
                        children.append(sym)

    return Symbol(
        name=name,
        kind="class",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=signature,
        children=children,
    )


def _python_constant(node, source: bytes) -> Symbol | None:
    """Build a Symbol for a Python module-level constant (ALL_CAPS)."""
    if not node.children or node.children[0].type != "identifier":
        return None
    name = _text(node.children[0], source)
    if not name.isupper():
        return None
    return Symbol(
        name=name,
        kind="constant",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=name,
    )


# ---------------------------------------------------------------------------
# JavaScript/TypeScript extractor (shared for JS, TS, TSX)
# ---------------------------------------------------------------------------


def _extract_javascript(root, source: bytes) -> list[Symbol]:
    """Extract symbols from JavaScript/TypeScript AST."""
    symbols = []
    for node in root.children:
        if node.type in ("function_declaration", "generator_function_declaration"):
            symbols.append(_js_function(node, source))
        elif node.type == "class_declaration":
            symbols.append(_js_class(node, source))
        elif node.type == "export_statement":
            for child in node.children:
                if child.type in ("function_declaration", "generator_function_declaration"):
                    symbols.append(_js_function(child, source))
                elif child.type == "class_declaration":
                    symbols.append(_js_class(child, source))
                elif child.type == "lexical_declaration":
                    symbols.extend(_js_variable(child, source))
        elif node.type == "lexical_declaration":
            symbols.extend(_js_variable(node, source))
    return symbols


def _js_function(node, source: bytes) -> Symbol:
    """Build a Symbol for a JS/TS function_declaration node."""
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source) if name_node else "anonymous"
    params_node = node.child_by_field_name("parameters")
    params = _text(params_node, source) if params_node else "()"
    ret_node = node.child_by_field_name("return_type")
    ret = f": {_text(ret_node, source)}" if ret_node else ""
    return Symbol(
        name=name,
        kind="function",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=f"function {name}{params}{ret}",
    )


def _js_class(node, source: bytes) -> Symbol:
    """Build a Symbol for a JS/TS class_declaration node."""
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source) if name_node else "anonymous"
    children = []
    body = node.child_by_field_name("body")
    if body:
        for child in body.children:
            if child.type == "method_definition":
                mname_node = child.child_by_field_name("name")
                mname = _text(mname_node, source) if mname_node else "?"
                params_node = child.child_by_field_name("parameters")
                params = _text(params_node, source) if params_node else "()"
                children.append(
                    Symbol(
                        name=mname,
                        kind="method",
                        line=child.start_point.row + 1,
                        end_line=child.end_point.row + 1,
                        signature=f"{mname}{params}",
                    )
                )
    return Symbol(
        name=name,
        kind="class",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=f"class {name}",
        children=children,
    )


def _js_variable(node, source: bytes) -> list[Symbol]:
    """Extract const/let that are arrow functions or class expressions."""
    symbols = []
    for child in node.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _text(name_node, source)
                value = child.child_by_field_name("value")
                if value and value.type in ("arrow_function", "function_expression", "class"):
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="function",
                            line=child.start_point.row + 1,
                            end_line=child.end_point.row + 1,
                            signature=f"const {name} = ...",
                        )
                    )
    return symbols


# ---------------------------------------------------------------------------
# Go extractor
# ---------------------------------------------------------------------------


def _extract_go(root, source: bytes) -> list[Symbol]:
    """Extract symbols from Go AST."""
    symbols = []
    for node in root.children:
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            result_node = node.child_by_field_name("result")
            name = _text(name_node, source) if name_node else "?"
            params = _text(params_node, source) if params_node else "()"
            ret = f" {_text(result_node, source)}" if result_node else ""
            symbols.append(
                Symbol(
                    name=name,
                    kind="function",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"func {name}{params}{ret}",
                )
            )
        elif node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            recv_node = node.child_by_field_name("receiver")
            name = _text(name_node, source) if name_node else "?"
            params = _text(params_node, source) if params_node else "()"
            recv = _text(recv_node, source) if recv_node else ""
            symbols.append(
                Symbol(
                    name=name,
                    kind="method",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"func {recv} {name}{params}",
                )
            )
        elif node.type == "type_declaration":
            for spec in node.children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    type_node = spec.child_by_field_name("type")
                    name = _text(name_node, source) if name_node else "?"
                    kind = (
                        "interface" if type_node and type_node.type == "interface_type" else "type"
                    )
                    symbols.append(
                        Symbol(
                            name=name,
                            kind=kind,
                            line=spec.start_point.row + 1,
                            end_line=spec.end_point.row + 1,
                            signature=f"type {name}",
                        )
                    )
    return symbols


# ---------------------------------------------------------------------------
# Rust extractor
# ---------------------------------------------------------------------------


def _extract_rust(root, source: bytes) -> list[Symbol]:
    """Extract symbols from Rust AST."""
    symbols = []
    for node in root.children:
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            ret_node = node.child_by_field_name("return_type")
            name = _text(name_node, source) if name_node else "?"
            params = _text(params_node, source) if params_node else "()"
            ret = f" -> {_text(ret_node, source)}" if ret_node else ""
            symbols.append(
                Symbol(
                    name=name,
                    kind="function",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"fn {name}{params}{ret}",
                )
            )
        elif node.type in ("struct_item", "enum_item"):
            name_node = node.child_by_field_name("name")
            name = _text(name_node, source) if name_node else "?"
            kind = "struct" if node.type == "struct_item" else "enum"
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"{kind} {name}",
                )
            )
        elif node.type == "impl_item":
            type_node = node.child_by_field_name("type")
            type_name = _text(type_node, source) if type_node else "?"
            children = []
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    if child.type == "function_item":
                        fn_name_node = child.child_by_field_name("name")
                        fn_name = _text(fn_name_node, source) if fn_name_node else "?"
                        children.append(
                            Symbol(
                                name=fn_name,
                                kind="method",
                                line=child.start_point.row + 1,
                                end_line=child.end_point.row + 1,
                                signature=f"fn {fn_name}(...)",
                            )
                        )
            symbols.append(
                Symbol(
                    name=type_name,
                    kind="impl",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"impl {type_name}",
                    children=children,
                )
            )
    return symbols


# ---------------------------------------------------------------------------
# PHP extractor
# ---------------------------------------------------------------------------


def _extract_php(root, source: bytes) -> list[Symbol]:
    """Extract symbols from PHP AST."""
    symbols: list[Symbol] = []
    for node in root.children:
        _extract_php_node(node, source, symbols)
    return symbols


def _extract_php_node(node, source: bytes, symbols: list[Symbol]) -> None:
    """Recursively extract function and class symbols from PHP AST nodes."""
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        params_node = node.child_by_field_name("parameters")
        name = _text(name_node, source) if name_node else "?"
        params = _text(params_node, source) if params_node else "()"
        symbols.append(
            Symbol(
                name=name,
                kind="function",
                line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature=f"function {name}{params}",
            )
        )
    elif node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = _text(name_node, source) if name_node else "?"
        children = []
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "method_declaration":
                    mname_node = child.child_by_field_name("name")
                    mname = _text(mname_node, source) if mname_node else "?"
                    children.append(
                        Symbol(
                            name=mname,
                            kind="method",
                            line=child.start_point.row + 1,
                            end_line=child.end_point.row + 1,
                            signature=f"{mname}()",
                        )
                    )
        symbols.append(
            Symbol(
                name=name,
                kind="class",
                line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature=f"class {name}",
                children=children,
            )
        )
    else:
        for child in node.children:
            _extract_php_node(child, source, symbols)


# ---------------------------------------------------------------------------
# CSS extractor
# ---------------------------------------------------------------------------


def _extract_css(root, source: bytes) -> list[Symbol]:
    """Extract selectors from CSS."""
    symbols = []
    for node in root.children:
        if node.type == "rule_set":
            # Find the selectors child by type (not a named field in this grammar)
            selectors = None
            for child in node.children:
                if child.type == "selectors":
                    selectors = child
                    break
            if selectors:
                text = _text(selectors, source).strip()
                symbols.append(
                    Symbol(
                        name=text,
                        kind="selector",
                        line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=text,
                    )
                )
    return symbols


# ---------------------------------------------------------------------------
# HCL extractor
# ---------------------------------------------------------------------------


def _extract_hcl(root, source: bytes) -> list[Symbol]:
    """Extract resources/blocks from HCL/Terraform."""
    symbols = []
    # HCL grammar wraps everything in config_file > body > block
    # Walk into body nodes to find blocks
    _extract_hcl_blocks(root, source, symbols)
    return symbols


def _extract_hcl_blocks(node, source: bytes, symbols: list[Symbol]) -> None:
    """Recursively find block nodes in HCL AST."""
    for child in node.children:
        if child.type == "block":
            labels = [
                _text(c, source) for c in child.children if c.type in ("identifier", "string_lit")
            ]
            name = " ".join(labels)
            kind = labels[0] if labels else "block"
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line=child.start_point.row + 1,
                    end_line=child.end_point.row + 1,
                    signature=name,
                )
            )
        elif child.type in ("body", "config_file"):
            _extract_hcl_blocks(child, source, symbols)


# ---------------------------------------------------------------------------
# Register all extractors
# ---------------------------------------------------------------------------

_EXTRACTORS.update(
    {
        "python": _extract_python,
        "javascript": _extract_javascript,
        "typescript": _extract_javascript,
        "tsx": _extract_javascript,
        "php": _extract_php,
        "go": _extract_go,
        "rust": _extract_rust,
        "css": _extract_css,
        "hcl": _extract_hcl,
    }
)
