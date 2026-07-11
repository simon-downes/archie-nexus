# 012 — Web Fetch and Web Search Tool Helpers

## Objective

Add `web_fetch` and `web_search` async exec tools to the container toolset, giving the
model the ability to retrieve web page content and perform DuckDuckGo searches from
within exec'd Python.

## Context

Plan 007 established the exec tool architecture: async functions in
`agent/src/archie_agent/exec/tools/` decorated with `@tool(guidelines=(...))`, registered
into `_TOOLS` dict, injected into the runner subprocess namespace. The runner subprocess
runs inside Docker with network access.

Reference implementation: nextgen `sandbox/capabilities/web.py` (203 lines) with `httpx`
for HTTP and `ddgs` for search.

Key facts:
- `httpx` is available in the workspace but must be an explicit agent dependency
- `ddgs` (DuckDuckGo search) is a new dependency
- `get_all_tools()` triggers registration by importing submodules
- Adding new tools automatically updates the LLM-facing exec tool description

## Requirements

### web_fetch

- MUST accept `url: str`, `mode: str` (default `"selective"`), `search_terms: str | None`
  - AC: `web_fetch("https://example.com")` returns cleaned text
- MUST use `httpx.AsyncClient` with follow_redirects=True, 30s timeout,
  User-Agent `Mozilla/5.0 (compatible; Archie/1.0)`
- MUST reject non-http(s) URLs with `ToolError`
- MUST reject binary content types with `ContentTypeError` (new ToolError subclass)
- MUST allow text-like types: `text/*`, `application/json`, `application/xml`,
  `application/javascript`, `application/xhtml+xml`, `application/rss+xml`,
  `application/atom+xml`, `application/ld+json`, `application/x-yaml`, `application/yaml`
- MUST strip HTML: remove script/style/nav/header/footer, strip tags, decode entities,
  collapse blank lines
  - AC: `<p>Hello</p><script>x</script>` → `Hello`
- MUST implement three modes:
  - `selective`: ~10 lines before/after search term matches; fall back to first 8000 chars
  - `truncated`: first 8000 characters
  - `full`: complete cleaned content
- MUST raise `ConnectionError` on network failures and non-2xx responses
- MUST raise `TimeoutError` on timeout
- MUST return `str`

### web_search

- MUST accept `query: str`, return `list[dict]` with keys `title`, `url`, `snippet`
  - AC: Results contain dicts with exactly those three string keys
- MUST use `ddgs` library with `safesearch="off"`, `backend="auto"`, max 8 results
- MUST return empty list on empty/whitespace query
- MUST return empty list (not raise) on any search exception
- SHOULD strip whitespace from query before searching

### Registration

- MUST register both via `@tool(guidelines=(...))` in new `web.py`
- MUST add `web` to import list in `get_all_tools()`
- MUST add `ContentTypeError` to `ToolError` hierarchy in `__init__.py`
- MUST add `ddgs>=9,<10` and `httpx>=0.27` to `agent/pyproject.toml`
- MUST pass ruff check and all tests

## Technical Design

### New file: `agent/src/archie_agent/exec/tools/web.py`

Port from nextgen with nexus conventions:
- `@capability` → `@tool(guidelines=(...))`
- Deferred imports for `httpx` and `ddgs` inside functions (fast module load)
- Private helpers: `_is_binary_content_type()`, `_html_to_text()`, `_extract_selective()`

### Error hierarchy

```python
class ContentTypeError(ToolError):
    """URL returned non-text content."""
```

`ConnectionError` and `TimeoutError` are builtins — not `ToolError` subclasses but the
runner catches all `Exception` and includes type+traceback.

### Registration

```python
from archie_agent.exec.tools import fs, shell, web  # noqa: F401
```

### Dependencies (`agent/pyproject.toml`)

```
"ddgs>=9,<10",
"httpx>=0.27",
```

### Guidelines

```python
@tool(guidelines=("Use `web_fetch` to retrieve content from a URL.",))
async def web_fetch(...): ...

@tool(guidelines=("Use `web_search` to find information on the web.",))
async def web_search(...): ...
```

## Milestones

### 1. ContentTypeError and dependency wiring

**Approach:**
Add exception, update `get_all_tools()` imports, add dependencies.

**Tasks:**
- Add `ContentTypeError(ToolError)` to `__init__.py`
- Add `web` to `get_all_tools()` import list
- Add `ddgs>=9,<10` and `httpx>=0.27` to `agent/pyproject.toml`
- Run `uv sync`

**Deliverable:** `ContentTypeError` importable, deps resolved.

**Verify:** `uv sync && uv run python -c "from archie_agent.exec.tools import ContentTypeError"`

---

### 2. Implement web_fetch and web_search

**Approach:**
Port nextgen implementation. `web_fetch`: validate URL, httpx GET, check content type,
clean HTML, apply mode. `web_search`: validate non-empty, DDGS.text(), map results,
catch all exceptions.

**Tasks:**
- Create `web.py` with both tools and private helpers
- `web_fetch`: URL validation, async httpx, content type check, HTML cleaning, modes
- `web_search`: strip/validate query, DDGS call, result mapping, exception handling
- `_is_binary_content_type()`, `_html_to_text()`, `_extract_selective()`

**Edge Cases:**
- Empty URL → `ConnectionError`
- Non-http(s) → `ConnectionError`
- Binary content type → `ContentTypeError`
- HTTP 4xx/5xx → `ConnectionError`
- Timeout → `TimeoutError`
- DNS failure → `ConnectionError`
- Selective with no matches → fall back to truncated
- Selective with no search_terms → fall back to truncated
- web_search empty query → `[]`
- web_search DDGS exception → `[]`

**Deliverable:** Both tools registered in `get_all_tools()`.

**Verify:** `uv run ruff check agent/src/archie_agent/exec/tools/web.py`

---

### 3. Unit tests

**Approach:**
Mock `httpx.AsyncClient` and `ddgs.DDGS` — no real network calls.

**Tasks:**
- Create `tests/test_web_tools.py`
- web_fetch tests: successful fetch, all three modes, error conditions, HTML cleaning
- web_search tests: success, empty query, exception, no results
- Registration tests: both in `get_all_tools()`, both have `_guidelines`
- Direct tests for `_html_to_text` and `_is_binary_content_type`

**Deliverable:** Comprehensive test suite passing without network access.

**Verify:** `uv run pytest tests/test_web_tools.py -v`

---

### 4. Full verification

**Approach:**
Run complete suite, confirm registration and guidelines integration.

**Tasks:**
- Run `uv run pytest` (all tests)
- Run `uv run ruff check`
- Verify `get_tool_guidelines()` includes web tool guidelines
- Verify `get_all_tools()` returns 8 tools (read, write, edit, grep, glob, shell,
  web_fetch, web_search)

**Deliverable:** All tests green, lint clean, fully integrated.

**Verify:** `uv run ruff check && uv run pytest -q`
