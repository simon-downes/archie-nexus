"""Brain browser pages under ``/brain``.

A read-only view of the brain root (``archie_shared.brain.brain_root``) as the host
sees it: a sidebar tree of directories and files, and a main pane that renders one
file. Markdown goes through :func:`transcript.render_markdown` (raw HTML escaped,
Pygments fences); a valid entry's frontmatter is shown as a header. The tree and file
views only read: they never create the root or touch ``index.yaml``.

The tree, and every path the browser opens, leaves out dot-names (``.git``, temporary
write files), the root ``index.yaml`` (derived) and symlinks (they could leave the root).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from archie_shared.brain import brain_root, parse_entry
from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import HTMLResponse

from archie_orchestrator import transcript
from archie_orchestrator.web import VIEWER_CSP, _render_viewer, templates

log = logging.getLogger(__name__)

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


def _file_view(root: Path, path: Path, rel: str) -> dict:
    """Main-pane context for one file."""
    text = path.read_text(encoding="utf-8")
    view: dict = {"view": "file", "rel": rel, "name": path.name}
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


def _landing() -> str:
    return _page({"view": "landing"})


async def index_page(request: Request) -> HTMLResponse:
    """GET /brain — the tree and a landing pane."""
    return _response(await asyncio.to_thread(_landing))


def _path_body(rel: str) -> tuple[str, int]:
    """Render ``/brain/<rel>``; a path that can't be shown gets the 404 pane."""
    root = brain_root()
    path = resolve_brain_path(rel)
    if path is not None and path.is_file():
        try:
            main = _file_view(root, path, path.relative_to(root).as_posix())
        except (OSError, UnicodeError) as exc:
            log.info("Brain browser: cannot read %s: %s", rel, exc)
        else:
            return _page(main, current=main["rel"]), 200
    return _page({"view": "missing", "rel": rel}), 404


async def path_page(request: Request) -> HTMLResponse:
    """GET /brain/{path} — one file; ``/brain/`` is the landing page."""
    rel = request.path_params["path"]
    if not rel.strip("/"):
        return await index_page(request)
    body, status = await asyncio.to_thread(_path_body, rel)
    return _response(body, status)
