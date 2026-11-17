"""Tests for the brain browser pages (GET /brain, /brain/{path})."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.web import VIEWER_CSP
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import serve


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    app.state.session_index = None


@pytest.fixture
def brain(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "brain"
    root.mkdir()
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(root))
    return root


def _entry(name: str, *, tags=("tag",), summary: str | None = None, body: str = "") -> str:
    tag_list = ", ".join(tags)
    return (
        f"---\nname: {name}\nsummary: {summary or f'summary of {name}'}\n"
        f"tags: [{tag_list}]\nupdated: '2026-11-10T18:04:20+00:00'\n---\n{body}"
    )


def _write(root: Path, rel: str, content: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


async def _get(path: str, headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


def _tree(html: str) -> str:
    return re.search(r'<nav class="brain-tree".*?</nav>', html, re.S).group(0)


def _main(html: str) -> str:
    return re.search(r'<section id="brain-main".*?</section>', html, re.S).group(0)


def _tree_links(html: str) -> list[str]:
    return re.findall(r'<a class="(?:dir|file)" href="([^"]+)"', _tree(html))


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tree_sorts_directories_first_and_skips_hidden_index_and_symlinks(brain, tmp_path):
    _write(brain, "a/z.md", "z")
    _write(brain, "a/B.md", "b")
    _write(brain, "c.md", "c")
    _write(brain, "Dir2/x.txt", "x")
    _write(brain, ".git/config", "x")
    _write(brain, ".tmp-write", "x")
    _write(brain, "index.yaml", "entries: []")
    _write(brain, "a/index.yaml", "nested index is a normal file")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    (brain / "link").symlink_to(outside)
    (brain / "filelink.md").symlink_to(outside / "secret.md")

    resp = await _get("/brain")

    assert resp.status_code == 200
    assert _tree_links(resp.text) == [
        "/brain/a",
        "/brain/a/B.md",
        "/brain/a/index.yaml",
        "/brain/a/z.md",
        "/brain/Dir2",
        "/brain/Dir2/x.txt",
        "/brain/c.md",
    ]


@pytest.mark.asyncio
async def test_tree_marks_current_file_and_opens_its_ancestors(brain):
    _write(brain, "a/b/entry.md", _entry("entry"))
    _write(brain, "other/x.md", "x")

    resp = await _get("/brain/a/b/entry.md")

    tree = _tree(resp.text)
    assert re.search(r'href="/brain/a/b/entry.md" aria-current="page"', tree)
    assert tree.count("aria-current") == 1
    assert re.search(r'<details open>\s*<summary><a class="dir" href="/brain/a"', tree)
    assert re.search(r'<details open>\s*<summary><a class="dir" href="/brain/a/b"', tree)
    assert re.search(r'<details>\s*<summary><a class="dir" href="/brain/other"', tree)


@pytest.mark.asyncio
async def test_tree_links_are_url_quoted(brain):
    _write(brain, "my notes/a #1.md", "x")
    resp = await _get("/brain")
    assert "/brain/my%20notes/a%20%231.md" in _tree_links(resp.text)
    page = await _get("/brain/my%20notes/a%20%231.md")
    assert page.status_code == 200


@pytest.mark.asyncio
async def test_missing_root_renders_empty_without_creating_it(tmp_path, monkeypatch):
    root = tmp_path / "no-brain"
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(root))

    resp = await _get("/brain")

    assert resp.status_code == 200
    assert "The brain is empty." in resp.text
    assert not root.exists()


@pytest.mark.asyncio
async def test_empty_path_is_the_landing_page(brain):
    resp = await _get("/brain/")
    assert resp.status_code == 200
    assert "Pick a file from the tree" in resp.text


# ---------------------------------------------------------------------------
# Markdown files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_renders_markdown_with_header_and_highlighting(brain):
    body = "# Heading\n\nSome *text* <script>alert(1)</script>\n\n```python\ndef f():\n    return 1\n```\n"
    _write(brain, "notes/entry.md", _entry("My entry", tags=("alpha", "beta"), body=body))

    resp = await _get("/brain/notes/entry.md")

    assert resp.status_code == 200
    main = _main(resp.text)
    assert "<h1>My entry</h1>" in main
    assert "summary of My entry" in main
    assert '<span class="tag">alpha</span><span class="tag">beta</span>' in main
    assert "2026-11-10 18:04 UTC" in main
    assert "summary:" not in main  # the YAML itself is not shown
    assert "<em>text</em>" in main
    assert "<script>" not in main
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in main
    assert '<pre class="highlight"><code><span class="k">def</span>' in main
    assert "/static/viewer/pygments.css" in resp.text
    assert "Not indexed" not in main
    assert "<title>My entry — Archie</title>" in resp.text


@pytest.mark.asyncio
async def test_markdown_without_frontmatter_shows_not_indexed_notice(brain):
    _write(brain, "loose.md", "# Loose\n\nno frontmatter")
    resp = await _get("/brain/loose.md")
    main = _main(resp.text)
    assert "Not indexed" in main
    assert "<h1>Loose</h1>" in main


@pytest.mark.asyncio
async def test_root_guidance_files_have_no_notice(brain):
    _write(brain, "SOUL.md", "# Soul\n\nvoice")
    _write(brain, "BRAIN.md", "# Brain rules")
    soul = await _get("/brain/SOUL.md")
    rules = await _get("/brain/BRAIN.md")
    assert soul.status_code == rules.status_code == 200
    assert "Not indexed" not in soul.text
    assert "Not indexed" not in rules.text
    assert "<h1>Soul</h1>" in _main(soul.text)


@pytest.mark.asyncio
async def test_relative_markdown_links_resolve_under_brain(brain):
    _write(brain, "a/one.md", _entry("one", body="See [two](two.md)."))
    resp = await _get("/brain/a/one.md")
    # Relative to /brain/a/one.md, so the browser opens /brain/a/two.md.
    assert '<a href="two.md">two</a>' in _main(resp.text)


# ---------------------------------------------------------------------------
# Paths that can't be shown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/brain/missing.md",
        "/brain/.git/config",
        "/brain/a/%2e%2e/%2e%2e/etc/passwd",
        "/brain/a/%2E%2E/c.md",
        "/brain/index.yaml",
        "/brain/link/secret.md",
        "/brain/filelink.md",
        "/brain/a//c.md",
        "/brain/a%00.md",
    ],
)
async def test_paths_that_cannot_be_shown_are_404(brain, tmp_path, path):
    _write(brain, "c.md", "c")
    _write(brain, "a/x.md", "x")
    _write(brain, ".git/config", "[core]")
    _write(brain, "index.yaml", "entries: []")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("top secret")
    (brain / "link").symlink_to(outside)
    (brain / "filelink.md").symlink_to(outside / "secret.md")

    resp = await _get(path)

    assert resp.status_code == 404
    assert "Not found" in _main(resp.text)
    assert "top secret" not in resp.text
    assert "[core]" not in resp.text
    assert resp.headers["content-security-policy"] == VIEWER_CSP


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csp_and_assets(brain):
    _write(brain, "c.md", "c")
    for path in ("/brain", "/brain/c.md"):
        resp = await _get(path)
        assert resp.headers["content-security-policy"] == VIEWER_CSP
        assert "/static/brain/brain.css" in resp.text
        assert "/static/brain/brain.js" in resp.text
        assert '<a class="brand" href="/brain">Archie <span>brain</span></a>' in resp.text
    css = await _get("/static/brain/brain.css")
    js = await _get("/static/brain/brain.js")
    assert css.status_code == js.status_code == 200


@pytest.mark.asyncio
async def test_nav_links_to_brain():
    with serve(app, {}):
        logs = await _get("/sessions")
        console = await _get("/")
    assert '<a href="/brain">Brain</a>' in logs.text
    assert '<a href="/brain">Brain</a>' in console.text
    assert '<a class="brand" href="/sessions">Archie <span>session logs</span></a>' in logs.text
