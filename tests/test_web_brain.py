"""Tests for the brain browser pages (GET /brain, /brain/{path})."""

from __future__ import annotations

import os
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


@pytest.mark.asyncio
async def test_another_name_for_the_root_index_is_404(brain):
    # A hard link stands in for a case-insensitive alias such as INDEX.yaml on macOS.
    _write(brain, "index.yaml", "entries: []")
    os.link(brain / "index.yaml", brain / "INDEX.yaml")
    resp = await _get("/brain/INDEX.yaml")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unreadable_file_is_404_and_unreadable_directory_is_skipped(brain):
    locked = _write(brain, "locked.md", "secret")
    _write(brain, "closed/inner.md", "inner")
    _write(brain, "open.md", "open")
    locked.chmod(0)
    (brain / "closed").chmod(0)
    try:
        file_resp = await _get("/brain/locked.md")
        tree_resp = await _get("/brain")
    finally:
        locked.chmod(0o644)
        (brain / "closed").chmod(0o755)

    assert file_resp.status_code == 404
    assert tree_resp.status_code == 200
    assert _tree_links(tree_resp.text) == ["/brain/closed", "/brain/locked.md", "/brain/open.md"]


# ---------------------------------------------------------------------------
# Other files and directories
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_markdown_file_is_highlighted_by_filename(brain):
    _write(brain, "tools/run.py", "import os\n\ndef main():\n    return os.getcwd()\n")
    _write(brain, "tools/notes.unknownext", "plain <b>text</b>")

    py = _main((await _get("/brain/tools/run.py")).text)
    plain = _main((await _get("/brain/tools/notes.unknownext")).text)

    assert '<div class="code"><pre class="highlight"><code><span class="kn">import</span>' in py
    assert '<h1 class="file-name">run.py</h1>' in py
    assert "Not indexed" not in py
    assert "plain &lt;b&gt;text&lt;/b&gt;" in plain
    assert "<b>" not in plain


@pytest.mark.asyncio
async def test_binary_large_and_empty_files_show_a_notice(brain):
    _write(brain, "bin.dat", b"abc\x00SECRETBYTES")
    _write(brain, "latin.txt", b"caf\xe9 SECRETBYTES")
    _write(brain, "big.txt", b"SECRETBYTES" + b"x" * (1024 * 1024 + 1 - 11))
    _write(brain, "limit.txt", b"y" * (1024 * 1024))
    _write(brain, "empty.md", "")

    binary = _main((await _get("/brain/bin.dat")).text)
    latin = _main((await _get("/brain/latin.txt")).text)
    big = _main((await _get("/brain/big.txt")).text)
    limit = await _get("/brain/limit.txt")
    empty = _main((await _get("/brain/empty.md")).text)

    assert "Binary file (15 bytes)." in binary
    assert "Binary file (16 bytes)." in latin
    assert "This file is too large to show (1.0 MiB)." in big
    for main in (binary, latin, big):
        assert "SECRETBYTES" not in main
    assert limit.status_code == 200 and "too large" not in limit.text  # exactly 1 MiB shows
    assert "Empty file." in empty


@pytest.mark.asyncio
async def test_directory_lists_children_with_tree_exclusions(brain, tmp_path):
    _write(brain, "a/z.md", "z")
    _write(brain, "a/B.md", "b")
    _write(brain, "a/sub/deep.md", "d")
    _write(brain, "a/.hidden", "h")
    (brain / "a" / "link.md").symlink_to(brain / "a" / "z.md")
    (brain / "empty").mkdir()

    resp = await _get("/brain/a")
    trailing = await _get("/brain/a/")
    empty = await _get("/brain/empty")

    assert resp.status_code == trailing.status_code == 200
    main = _main(resp.text)
    assert re.findall(r'<li><a class="(?:dir|file)" href="([^"]+)"', main) == [
        "/brain/a/sub",
        "/brain/a/B.md",
        "/brain/a/z.md",
    ]
    assert "deep.md" not in main  # not recursive
    assert re.search(r'href="/brain/a" aria-current="page"', _tree(resp.text))
    assert "<title>a — Archie</title>" in resp.text
    assert "Empty directory." in _main(empty.text)


@pytest.mark.asyncio
async def test_breadcrumbs_link_each_ancestor(brain):
    _write(brain, "a/b/entry.md", _entry("entry"))

    main = _main((await _get("/brain/a/b/entry.md")).text)
    crumbs = re.search(r'<p class="crumbs chrome">.*?</p>', main, re.S).group(0)

    assert re.findall(r'href="([^"]+)"', crumbs) == ["/brain", "/brain/a", "/brain/a/b"]
    assert '<span aria-current="location">entry.md</span>' in crumbs


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_fixture(brain: Path, tmp_path: Path) -> None:
    _write(brain, "a/name-match.md", _entry("Deploy guide", tags=("ops",), body="steps"))
    _write(brain, "b/tag-match.md", _entry("Runbook", tags=("deploy",), body="other"))
    _write(brain, "c/body-match.md", _entry("Notes", body="intro\nhow we Deploy & ship\n"))
    _write(brain, "d/none.md", _entry("Unrelated", body="nothing here"))
    _write(brain, "loose.md", "deploy without frontmatter")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "linked.md").write_text(_entry("Linked deploy", body="x"))
    (brain / "linked.md").symlink_to(outside / "linked.md")
    _write(brain, "real.md", _entry("Real deploy target", body="x"))
    (brain / "alias.md").symlink_to(brain / "real.md")


def _hit_paths(html: str) -> list[str]:
    return re.findall(r'<li class="hit" data-path="([^"]+)"', html)


@pytest.mark.asyncio
async def test_search_matches_brain_search_order(brain, tmp_path):
    from archie_shared.brain import search

    _search_fixture(brain, tmp_path)

    resp = await _get("/brain?q=deploy")

    assert resp.status_code == 200
    expected = [hit["path"] for hit in search("deploy", 50)]
    assert len(expected) >= 4
    assert _hit_paths(resp.text) == expected
    assert "loose.md" not in _hit_paths(resp.text)
    assert 'value="deploy"' in resp.text  # the box keeps the query
    assert re.search(
        r'<a class="hit-name" href="/brain/a/name-match.md">Deploy guide</a>', resp.text
    )


@pytest.mark.asyncio
async def test_search_hit_shows_summary_tags_and_marked_excerpt(brain, tmp_path):
    _search_fixture(brain, tmp_path)

    main = _main((await _get("/brain?q=deploy")).text)
    hit = re.search(r'<li class="hit" data-path="c/body-match.md">.*?</li>', main, re.S).group(0)

    assert "summary of Notes" in hit
    assert '<span class="tag">tag</span>' in hit
    assert '<p class="hit-excerpt">how we <mark>Deploy</mark> &amp; ship</p>' in hit
    assert re.search(r'<span class="score" [^>]*>\d+\.\d</span>', hit)


@pytest.mark.asyncio
async def test_search_excerpt_marks_never_split_entities(brain):
    _write(brain, "e.md", _entry("Entity", body="fish & chips <amp>\n"))

    main = _main((await _get("/brain?q=amp")).text)

    assert "fish &amp; chips &lt;<mark>amp</mark>&gt;" in main


@pytest.mark.asyncio
async def test_search_hits_the_browser_cannot_show_are_unlinked(brain, tmp_path):
    _search_fixture(brain, tmp_path)

    main = _main((await _get("/brain?q=real")).text)

    alias = re.search(r'<li class="hit" data-path="alias.md">.*?</li>', main, re.S).group(0)
    real = re.search(r'<li class="hit" data-path="real.md">.*?</li>', main, re.S).group(0)
    assert "href=" not in alias
    assert "(not browsable)" in alias
    assert 'href="/brain/real.md"' in real


@pytest.mark.asyncio
async def test_search_without_matches_and_blank_query(brain, tmp_path):
    _search_fixture(brain, tmp_path)

    none = await _get("/brain?q=zebra")
    blank = await _get("/brain?q=%20%20")

    assert "No entries match <code>zebra</code>." in _main(none.text)
    assert "Pick a file from the tree" in _main(blank.text)


@pytest.mark.asyncio
async def test_search_query_is_escaped(brain):
    _write(brain, "x.md", _entry("x"))
    resp = await _get("/brain?q=%3Cscript%3E")
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in _main(resp.text)


@pytest.mark.asyncio
async def test_search_failure_shows_a_notice(brain):
    _write(brain, "ok.md", _entry("ok", body="x"))
    locked = _write(brain, "locked.md", _entry("locked", body="x"))
    locked.chmod(0)  # rg reports the unreadable file and exits 2
    try:
        resp = await _get("/brain?q=x")
    finally:
        locked.chmod(0o644)
    assert resp.status_code == 200
    assert "Search failed: ripgrep error:" in _main(resp.text)


@pytest.mark.asyncio
async def test_search_that_cannot_start_rg_shows_a_notice(brain, monkeypatch):
    _write(brain, "ok.md", _entry("ok", body="x"))

    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "rg")

    monkeypatch.setattr("archie_shared.brain.subprocess.run", missing)
    resp = await _get("/brain?q=x")
    assert resp.status_code == 200
    assert "Search failed:" in _main(resp.text) and "No such file or directory" in resp.text


@pytest.mark.asyncio
async def test_search_never_writes_the_index(brain, tmp_path, monkeypatch):
    _write(brain, "a.md", _entry("alpha", body="x"))

    resp = await _get("/brain?q=alpha")

    assert _hit_paths(resp.text) == ["a.md"]
    assert not (brain / "index.yaml").exists()
    assert not (brain.parent / f".{brain.name}.brain.lock").exists()

    missing = tmp_path / "no-brain"
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(missing))
    resp = await _get("/brain?q=alpha")
    assert "No entries match" in _main(resp.text)
    assert not missing.exists()


@pytest.mark.asyncio
async def test_htmx_search_returns_main_fragment(brain, tmp_path):
    _search_fixture(brain, tmp_path)

    resp = await _get("/brain?q=deploy", headers={"HX-Request": "true"})
    restore = await _get(
        "/brain?q=deploy", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
    )
    landing = await _get("/brain", headers={"HX-Request": "true"})

    body = resp.text.strip()
    assert body.startswith('<section id="brain-main"')
    assert "<html" not in body
    assert _hit_paths(body)
    assert resp.headers["vary"] == "HX-Request, HX-History-Restore-Request"
    assert resp.headers["content-security-policy"] == VIEWER_CSP
    assert "<html" in restore.text
    assert landing.text.strip().startswith('<section id="brain-main"')
    assert "Pick a file from the tree" in landing.text


@pytest.mark.asyncio
async def test_search_box_attributes(brain):
    resp = await _get("/brain")
    box = re.search(r'<input type="search"[^>]*>', resp.text, re.S).group(0)
    assert 'name="q"' in box
    assert 'hx-get="/brain"' in box
    assert 'hx-target="#brain-main"' in box
    assert 'hx-swap="outerHTML"' in box
    assert 'hx-push-url="true"' in box
    assert "delay:250ms" in box


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
