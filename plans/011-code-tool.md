# 011 — Code Tool: tree-sitter structural code intelligence

## Objective

Add a `code` exec tool that provides tree-sitter-based structural code intelligence —
extracting symbol definitions (functions, classes, methods, types) from source files.
This gives the model the ability to understand codebase structure without reading entire
files, enabling faster navigation, symbol search, and context gathering.

## Context

Plan 007 established the exec tool framework: `@tool` registration, `ToolError` hierarchy,
`get_all_tools()`, and the runner/harness integration. The `code` tool slots into
`agent/src/archie_agent/exec/tools/` alongside `fs.py` and `shell.py`.

A reference implementation exists in nextgen (`sandbox/capabilities/code.py`, 898 lines)
supporting 9 languages via tree-sitter with lazy parser loading, file discovery via ripgrep,
and a `Symbol` dataclass for structured output.

## Requirements

### Functional

- MUST support file mode: parse a single file, return all symbols
  - AC: `await code(path="src/main.py")` returns a list of symbol dicts
- MUST support directory mode: scan recursively, return symbols from all supported files
  - AC: `await code(path="src/")` returns symbols from all supported files under `src/`
- MUST support name search mode: filter by case-insensitive substring (including children)
  - AC: `await code(name="parse")` returns only matching symbols
- MUST support 9 languages: Python, JavaScript, TypeScript, TSX, PHP, Go, Rust, CSS, HCL
  - AC: Each supported extension is parsed without error
- MUST map extensions: `.py`→python, `.js`/`.mjs`→javascript, `.ts`→typescript, `.tsx`→tsx,
  `.php`→php, `.go`→go, `.rs`→rust, `.css`/`.scss`→css, `.tf`/`.hcl`→hcl
- MUST accept optional `language` filter to restrict file discovery
- MUST return symbols as list of dicts: `name`, `kind`, `line`, `end_line`, `signature`,
  optional `children`, optional `file` (in directory/search modes)
- MUST extract appropriate symbol kinds per language:
  - Python: function, class, method, constant
  - JS/TS/TSX: function, class, method
  - Go: function, method, type, interface
  - Rust: function, struct, enum, impl, method
  - PHP: function, class, method
  - CSS: selector
  - HCL: block (resource, variable, module, etc.)
- MUST resolve paths relative to WORKSPACE with same validation as `fs.py`
- MUST raise `PathValidationError` for paths outside `/workspace/`
- MUST raise `FileNotFoundError` for nonexistent paths
- MUST raise `UnsupportedLanguageError` (new) for unrecognised extensions
- MUST raise `FileTooLargeError` (new) for files exceeding 500KB
- MUST raise `BinaryFileError` for binary files
- MUST silently skip erroring files in directory mode
- MUST use `rg --files` for discovery (respects .gitignore), fallback to pathlib.rglob
- MUST skip: node_modules, .venv, venv, __pycache__, build, dist, .git, .tox
- MUST cap search results at 50 symbols
- MUST use lazy parser initialization (no import on module load)

### Non-functional

- MUST add tree-sitter + 8 grammar packages to `agent/pyproject.toml`
- MUST register via `@tool(guidelines=(...))` with appropriate guideline text
- MUST update `get_all_tools()` to import the `code` module
- MUST pass ruff check and all tests

## Technical Design

### Module: `agent/src/archie_agent/exec/tools/code.py`

```python
@tool(guidelines=("Use `code` to explore symbol structure before reading full files.",))
async def code(
    path: str | None = None,
    name: str | None = None,
    language: str | None = None,
) -> list[dict]:
```

### Symbol dataclass

```python
@dataclass
class Symbol:
    name: str
    kind: str
    line: int       # 1-based
    end_line: int   # 1-based inclusive
    signature: str
    children: list[Symbol] = field(default_factory=list)

    def to_dict(self) -> dict: ...
```

### Exceptions (added to `exec/tools/__init__.py`)

```python
class UnsupportedLanguageError(ToolError): ...
class FileTooLargeError(ToolError): ...
```

### Language support

- `_EXTENSION_MAP`: file extension → language name
- `_LANGUAGE_LOADERS`: language → lazy loader function (imports grammar on first call)
- `_EXTRACTORS`: language → extractor function
- Parsers cached in module-level `_parsers` dict
- JS extractor reused for TS/TSX

### Path resolution

Import `WORKSPACE` from `archie_agent.exec.tools.fs`. Implement `_resolve_path()` with
same validation rules.

### File discovery

`_discover_files()` via `rg --files` (async subprocess), filtered by extension map.
Fallback to `pathlib.rglob("*")` with `_SKIP_DIRS` filtering.

### Dependencies (`agent/pyproject.toml`)

```
tree-sitter>=0.25
tree-sitter-python>=0.25.0
tree-sitter-javascript>=0.25.0
tree-sitter-typescript>=0.23.2
tree-sitter-go>=0.25.0
tree-sitter-rust>=0.24.2
tree-sitter-php>=0.24.1
tree-sitter-css>=0.25.0
tree-sitter-hcl>=1.2.0
```

## Milestones

### 1. Exception classes + registration wiring

**Approach:**
Add new exceptions, update `get_all_tools()` imports, add dependencies, create placeholder
`code.py` with a `@tool`-decorated function (raises `NotImplementedError`).

**Tasks:**
- Add `UnsupportedLanguageError` and `FileTooLargeError` to `__init__.py`
- Update `get_all_tools()`: `from archie_agent.exec.tools import fs, shell, code`
- Create `code.py` placeholder with `@tool` decoration
- Add 9 dependencies to `agent/pyproject.toml`
- Run `uv sync`

**Deliverable:** Exceptions importable, `get_all_tools()` includes `"code"`, deps installed.

**Verify:** `uv run python -c "from archie_agent.exec.tools import UnsupportedLanguageError"`.
`uv run ruff check` clean.

---

### 2. Core infrastructure (Symbol, parsers, path resolution, file discovery)

**Approach:**
Build `Symbol`, extension map, lazy loaders, path resolution, file discovery, filter/search
logic. Use `_extract_generic` (returns empty) as placeholder extractor.

**Edge Cases:**
- Unknown extension → `UnsupportedLanguageError`
- File > 500KB → `FileTooLargeError`
- Binary file → `BinaryFileError`
- Grammar missing → `UnsupportedLanguageError` (caught ImportError)

**Tasks:**
- Implement `Symbol` with `to_dict()`
- Implement `_EXTENSION_MAP`, `_LANGUAGE_LOADERS`, `_get_parser()`
- Implement `_resolve_path()` importing `WORKSPACE`
- Implement `_discover_files()` (rg + fallback)
- Implement `_filter_symbols()` and search cap
- Implement `_parse_file()` dispatch
- Wire `code()` function
- Add `tests/test_code_tool.py` for infrastructure (path validation, binary, size, discovery)

**Deliverable:** `code()` runs end-to-end, discovers files, validates paths, returns `[]`.

**Verify:** `uv run pytest tests/test_code_tool.py -v -k infrastructure` passes.

---

### 3. Language extractors (all 9 languages)

**Approach:**
Port per-language extractors from nextgen. Each walks tree-sitter AST and returns
`list[Symbol]`. Register in `_EXTRACTORS` dict.

**Tasks:**
- Port `_extract_python` (functions, classes+methods, decorators, ALL_CAPS constants)
- Port `_extract_javascript` (functions, classes, arrow functions) — reuse for TS/TSX
- Port `_extract_go` (functions, methods, types, interfaces)
- Port `_extract_rust` (functions, structs, enums, impl blocks)
- Port `_extract_php` (functions, classes+methods)
- Port `_extract_css` (selectors)
- Port `_extract_hcl` (blocks)
- Add per-language tests with fixture files

**Edge Cases:**
- Malformed files → error nodes naturally skipped
- Empty files → `[]`
- Comments-only files → `[]`

**Deliverable:** All 9 languages produce correct symbols.

**Verify:** `uv run pytest tests/test_code_tool.py -v -k extract` passes.

---

### 4. Integration: full `code()` function + end-to-end

**Approach:**
Verify complete tool works: callable from exec, appears in auto-generated docs, all tests pass.

**Tasks:**
- Verify `code` in `_generate_tool_docs()` output
- Add integration tests: multi-file dir scan, name search, language filter, combined
- Run full suite
- Verify `get_tool_guidelines()` includes code's guideline

**Deliverable:** Fully operational `code` tool, tested and integrated.

**Verify:** `uv run ruff check && uv run pytest -q` all green.
