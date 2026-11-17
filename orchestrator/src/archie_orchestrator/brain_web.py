"""Brain browser pages under ``/brain``.

A read-only view of the brain root (``archie_shared.brain.brain_root``) as the host
sees it: a sidebar tree of directories and files, and a main pane that renders one
file. Markdown goes through :func:`transcript.render_markdown` (raw HTML escaped,
Pygments fences); a valid entry's frontmatter is shown as a header. The tree and file
views only read: they never create the root or touch ``index.yaml``.

The tree, and every path the browser opens, leaves out dot-names (``.git``, temporary
write files), the root ``index.yaml`` (derived) and symlinks (they could leave the root).

``/brain?q=`` runs :func:`archie_shared.brain.search`, the function behind the agent's
``brain_search`` tool, so results and their order match it. Like the tool, a search may
refresh ``index.yaml``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from archie_shared.brain import BrainError, brain_root, parse_entry, search
from markupsafe import Markup, escape
from starlette.requests import Request
from starlette.responses import HTMLResponse

from archie_orchestrator import transcript
from archie_orchestrator.web import VIEWER_CSP, _render_viewer, templates

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1024 * 1024  # larger files show a notice instead of their content
SEARCH_LIMIT = 50  # search()'s own cap; the order is the brain_search tool's
_GUIDANCE_NAMES = {"BRAIN.md", "SOUL.md"}  # root guidance files: not entries
_MARKDOWN_SUFFIXES = {".md", ".markdown"}


def brain_href(rel: str) -> str:
    """The browser URL for a brain-relative POSIX path."""
    return "/brain/" + quote(rel) if rel else "/brain"


templates.env.globals["brain_href"] = brain_href


@dataclass(frozen=True)
class Node:
    """One directory or file in the sidebar tree."""

    name: str
    rel: str  # POSIX path from the brain root
    is_dir: bool
    children: tuple[Node, ...] = ()
    open: bool = False  # the current path is this directory or inside it
    current: bool = False


def _shown(root: Path, path: Path) -> bool:
    """Whether a directory entry belongs in the tree and listings."""
    if path.name.startswith(".") or path.is_symlink():
        return False
    return not (path.parent == root and path.name == "index.yaml")


def _sort_key(node: Node) -> tuple[bool, str, str]:
    return (not node.is_dir, node.name.lower(), node.name)


def _children(root: Path, directory: Path, current: str, *, deep: bool) -> tuple[Node, ...]:
    """Shown entries of ``directory``: directories first, then files, case-insensitively."""
    try:
        entries = list(directory.iterdir())
    except OSError:  # missing root, permission denied, removed while walking
        return ()
    nodes = []
    for path in entries:
        if not _shown(root, path):
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            nodes.append(
                Node(
                    path.name,
                    rel,
                    True,
                    _children(root, path, current, deep=True) if deep else (),
                    open=current == rel or current.startswith(rel + "/"),
                    current=current == rel,
                )
            )
        elif path.is_file():
            nodes.append(Node(path.name, rel, False, current=current == rel))
    return tuple(sorted(nodes, key=_sort_key))


def build_tree(root: Path, current: str = "") -> tuple[Node, ...]:
    """The whole tree under ``root``, with ``current`` (a relative path) marked."""
    return _children(root, root, current, deep=True)


def resolve_brain_path(rel: str) -> Path | None:
    """Return the file or directory for a browser path, or None when it can't be shown.

    Rejects empty and dot-name components (so ``..``), the root ``index.yaml``, missing
    paths and any path through a symlink: the resolved path must equal the lexical one.
    """
    root = brain_root()
    rel = rel.removesuffix("/")
    parts = rel.split("/")
    if not rel or any(not part or part.startswith(".") for part in parts):
        return None
    if parts == ["index.yaml"]:
        return None
    path = root.joinpath(*parts)
    try:
        if path.resolve(strict=True) != path:
            return None
    except (OSError, RuntimeError, ValueError):  # missing, a loop, an embedded NUL
        return None
    return path


def _format_updated(value: str) -> str:
    return value[:16].replace("T", " ") + " UTC"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} byte{'' if size == 1 else 's'}"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _crumbs(rel: str) -> list[tuple[str, str]]:
    """(name, href) for the brain root and each ancestor directory of ``rel``."""
    parts = rel.split("/")[:-1]
    return [("brain", "/brain")] + [
        (part, brain_href("/".join(parts[: i + 1]))) for i, part in enumerate(parts)
    ]


def _text(data: bytes) -> str | None:
    """Decode a file as UTF-8 text; None for binary (a NUL byte or invalid UTF-8)."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _file_view(root: Path, path: Path, rel: str) -> dict:
    """Main-pane context for one file."""
    view: dict = {"view": "file", "rel": rel, "name": path.name, "crumbs": _crumbs(rel)}
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return {**view, "notice": f"This file is too large to show ({_format_size(size)})."}
    text = _text(path.read_bytes())
    if text is None:
        return {**view, "notice": f"Binary file ({_format_size(size)})."}
    if not text:
        return {**view, "notice": "Empty file."}
    if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
        return {**view, "html": Markup(transcript.highlight_file(text, path.name))}
    entry = parse_entry(text)
    if entry is None:
        guidance = path.parent == root and path.name in _GUIDANCE_NAMES
        return {
            **view,
            "markdown": True,
            "unindexed": not guidance,
            "html": transcript.render_markdown(text),
        }
    meta = entry["metadata"]
    return {
        **view,
        "markdown": True,
        "meta": {
            "name": meta["name"],
            "summary": meta["summary"],
            "tags": meta["tags"],
            "updated": _format_updated(entry["updated"]),
        },
        "html": transcript.render_markdown(entry["body"]),
    }


def _dir_view(root: Path, path: Path, rel: str) -> dict:
    """Main-pane context for one directory: its shown children, not recursive."""
    return {
        "view": "dir",
        "rel": rel,
        "name": path.name,
        "crumbs": _crumbs(rel),
        "children": _children(root, path, "", deep=False),
    }


def _page(main: dict, *, current: str = "", q: str = "") -> str:
    """Render the full page: sidebar tree plus ``main``. Runs in a worker thread."""
    root = brain_root()
    return _render_viewer(
        "brain/page.html",
        {"tree": build_tree(root, current), "main": main, "q": q, "root": str(root)},
    )


def _response(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        body, status_code=status_code, headers={"Content-Security-Policy": VIEWER_CSP}
    )


def _mark(text: str, terms: list[str]) -> Markup:
    """Escape ``text`` and wrap each case-insensitive match of a term in ``<mark>``.

    Spans are found on the raw text, so a term like ``amp`` never matches an entity.
    """
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.IGNORECASE
    )
    out, last = [], 0
    for match in pattern.finditer(text):
        out.append(escape(text[last : match.start()]))
        out.append(Markup("<mark>%s</mark>") % match.group(0))
        last = match.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


def _hit(hit: dict, terms: list[str]) -> dict:
    """One search result for the template; ``href`` is None when the browser can't show it."""
    return {
        "path": hit["path"],
        "name": hit["name"],
        "summary": hit["summary"],
        "tags": hit["tags"],
        "updated": _format_updated(hit["updated"]),
        "score": f"{hit['score']:.1f}",
        "excerpt": _mark(hit["excerpt"], terms) if hit.get("excerpt") else None,
        "href": brain_href(hit["path"]) if resolve_brain_path(hit["path"]) else None,
    }


def _results_view(q: str) -> dict:
    """Main-pane context for a search, ranked by the ``brain_search`` function itself."""
    view: dict = {"view": "results", "q": q, "hits": []}
    try:
        hits = search(q, SEARCH_LIMIT)
    except (BrainError, OSError) as exc:  # rg failed, or the index can't be written
        log.warning("Brain search failed: %s", exc)
        return {**view, "error": str(exc)}
    terms = list(dict.fromkeys(q.lower().split()))  # as search() splits them
    return {**view, "hits": [_hit(hit, terms) for hit in hits]}


def _index_body(q: str, partial: bool) -> str:
    """Render ``/brain``: search results for a non-blank ``q``, else the landing pane."""
    main = _results_view(q) if q.strip() else {"view": "landing"}
    if partial:
        return _render_viewer("brain/_main.html", {"main": main, "root": str(brain_root())})
    return _page(main, q=q)


async def index_page(request: Request) -> HTMLResponse:
    """GET /brain — the tree with a landing pane, or search results for ``q``.

    An htmx request (``HX-Request``) gets only the ``#brain-main`` fragment; htmx
    history restores (``HX-History-Restore-Request``) get the full page.
    """
    q = request.query_params.get("q", "")
    partial = (
        request.headers.get("hx-request") == "true"
        and request.headers.get("hx-history-restore-request") != "true"
    )
    response = _response(await asyncio.to_thread(_index_body, q, partial))
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


def _path_body(rel: str) -> tuple[str, int]:
    """Render ``/brain/<rel>``; a path that can't be shown gets the 404 pane."""
    root = brain_root()
    path = resolve_brain_path(rel)
    if path is not None:
        shown = path.relative_to(root).as_posix()
        try:
            if path.is_dir():
                main = _dir_view(root, path, shown)
            elif path.is_file():
                main = _file_view(root, path, shown)
            else:  # a socket, FIFO or device
                main = None
        except OSError as exc:  # unreadable, or removed since it was resolved
            log.info("Brain browser: cannot read %s: %s", rel, exc)
            main = None
        if main is not None:
            return _page(main, current=shown), 200
    return _page({"view": "missing", "rel": rel}), 404


async def path_page(request: Request) -> HTMLResponse:
    """GET /brain/{path} — one file or directory; ``/brain/`` is the landing page."""
    rel = request.path_params["path"]
    if not rel.strip("/"):
        return await index_page(request)
    body, status = await asyncio.to_thread(_path_body, rel)
    return _response(body, status)
