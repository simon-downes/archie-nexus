"""Filesystem-backed curated brain index and ranked search service."""

from __future__ import annotations

import fcntl
import math
import os
import subprocess
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from archie_shared.config import home_dir

FRESHNESS_INTERVAL_SECONDS = 24 * 60 * 60
_ROOT_EXCLUDED_NAMES = {"index.yaml", "BRAIN.md"}
_LOCK_NAME = ".brain.lock"
_EXCLUDED_DIRS = {".git", ".hg", ".svn"}


class BrainError(Exception):
    """A brain configuration, validation, or indexing error."""


def brain_root() -> Path:
    raw = os.environ.get("ARCHIE_BRAIN_DIR")
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = home_dir() / p
    else:
        p = home_dir() / "brain"
    return p.resolve()


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    if not text.startswith("---") or not (len(text) == 3 or text[3] in "\r\n"):
        return None
    lines = text.splitlines(keepends=True)
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return None
    try:
        data = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data, "".join(lines[end + 1 :])


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def parse_entry(text: str) -> dict[str, Any] | None:
    try:
        parsed = _split_frontmatter(text)
    except (UnicodeError, ValueError):
        return None
    if parsed is None:
        return None
    metadata, body = parsed
    if not all(k in metadata for k in ("name", "summary", "tags", "updated")):
        return None
    if not isinstance(metadata["name"], str) or not isinstance(metadata["summary"], str):
        return None
    if not isinstance(metadata["tags"], list) or not all(
        isinstance(x, str) for x in metadata["tags"]
    ):
        return None
    dt = _timestamp(metadata["updated"])
    if dt is None:
        return None
    return {"metadata": metadata, "body": body, "updated": dt.isoformat(timespec="microseconds")}


def _lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}.brain.lock"


def _candidate(path: Path, root: Path) -> bool:
    if (
        (path.parent == root and path.name in _ROOT_EXCLUDED_NAMES)
        or path == _lock_path(root)
        or any(part in _EXCLUDED_DIRS for part in path.parts)
    ):
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path.is_file()


def _records(root: Path) -> list[dict[str, Any]]:
    result = []
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if not _candidate(path, root):
            continue
        try:
            parsed = parse_entry(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if parsed:
            rel = path.relative_to(root).as_posix()
            result.append(
                {
                    "path": rel,
                    "name": parsed["metadata"]["name"],
                    "summary": parsed["metadata"]["summary"],
                    "tags": parsed["metadata"]["tags"],
                    "updated": parsed["updated"],
                }
            )
    return sorted(result, key=lambda x: x["path"])


def _valid_index(data: Any, root: Path) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    seen = set()
    out = []
    for r in data["entries"]:
        if not isinstance(r, dict) or {"path", "name", "summary", "tags", "updated"} - r.keys():
            return None
        if not isinstance(r["path"], str) or r["path"] in seen or Path(r["path"]).is_absolute():
            return None
        p = (root / r["path"]).resolve()
        if not _candidate(p, root):
            return None
        try:
            p.relative_to(root)
        except ValueError:
            return None
        if (
            not isinstance(r["name"], str)
            or not isinstance(r["summary"], str)
            or not isinstance(r["tags"], list)
            or not all(isinstance(x, str) for x in r["tags"])
        ):
            return None
        if _timestamp(r["updated"]) is None:
            return None
        seen.add(r["path"])
        out.append(r)
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _write_index(root: Path, records: list[dict[str, Any]]) -> None:
    _atomic_write(root / "index.yaml", yaml.safe_dump({"entries": records}, sort_keys=False))


@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with _lock_path(root).open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def rebuild_index() -> list[dict[str, Any]]:
    root = brain_root()
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root):
        records = _records(root)
        _write_index(root, records)
    return records


def _load_index(root: Path) -> list[dict[str, Any]] | None:
    try:
        data = yaml.safe_load((root / "index.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    return _valid_index(data, root)


def _needs_refresh(root: Path, records: list[dict[str, Any]], index_mtime: float) -> bool:
    for r in records:
        if not (root / r["path"]).is_file():
            return True
    if (
        os.path.getmtime(root / "index.yaml") + FRESHNESS_INTERVAL_SECONDS
        > datetime.now().timestamp()
    ):
        return False
    for p in root.rglob("*"):
        if _candidate(p, root) and p.stat().st_mtime > index_mtime:
            return True
    return False


def ensure_fresh_index() -> list[dict[str, Any]]:
    root = brain_root()
    root.mkdir(parents=True, exist_ok=True)
    records = _load_index(root)
    try:
        stale = records is None or _needs_refresh(
            root, records, (root / "index.yaml").stat().st_mtime
        )
    except OSError:
        stale = True
    if not stale:
        return records or []
    with _lock(root):
        records = _load_index(root)
        try:
            stale = records is None or _needs_refresh(
                root, records, (root / "index.yaml").stat().st_mtime
            )
        except OSError:
            stale = True
        if stale:
            records = _records(root)
            _write_index(root, records)
    return records or []


def _render(metadata: dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(metadata, sort_keys=False).rstrip() + "\n---\n" + body


def edit_mutate(
    path: Path, old_text: str, new_text: str, replace_all: bool = False
) -> tuple[str, str]:
    root = brain_root()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        original = resolved.read_text(encoding="utf-8", errors="replace")
        count = original.count(old_text)
        if count == 0:
            raise BrainError("Text not found in file") from None
        if count > 1 and not replace_all:
            raise BrainError(f"Found {count} matches") from None
        return original, original.replace(old_text, new_text, 1 if not replace_all else -1)
    with _lock(root):
        original = resolved.read_text(encoding="utf-8", errors="replace")
        count = original.count(old_text)
        if count == 0:
            raise BrainError("Text not found in file")
        if count > 1 and not replace_all:
            raise BrainError(f"Found {count} matches")
        modified = original.replace(old_text, new_text, 1 if not replace_all else -1)
        return original, mutate(resolved, modified, _already_locked=True)


def mutate(path: Path, content: str, *, _already_locked: bool = False) -> str:
    root = brain_root()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return content
    root.mkdir(parents=True, exist_ok=True)
    lock_context = nullcontext() if _already_locked else _lock(root)
    with lock_context:
        old = resolved.read_bytes() if resolved.exists() else None
        old_index = (root / "index.yaml").read_bytes() if (root / "index.yaml").exists() else None
        try:
            existing = parse_entry(old.decode("utf-8")) if old is not None else None
        except UnicodeDecodeError:
            existing = None
        parsed = _split_frontmatter(content)
        structurally_valid = False
        if parsed:
            candidate_metadata, _ = parsed
            structurally_valid = (
                isinstance(candidate_metadata.get("name"), str)
                and isinstance(candidate_metadata.get("summary"), str)
                and isinstance(candidate_metadata.get("tags"), list)
                and all(isinstance(tag, str) for tag in candidate_metadata["tags"])
            )
        if parsed and structurally_valid:
            metadata, body = parsed
            metadata["updated"] = datetime.now(UTC).isoformat()
            content = _render(metadata, body)
        elif existing:
            raise BrainError("edit would remove required brain frontmatter")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(resolved, content)
            records = _load_index(root)
            if records is None:
                records = _records(root)
            relative = resolved.relative_to(root).as_posix()
            records = [record for record in records if record["path"] != relative]
            parsed_written = parse_entry(content)
            if parsed_written:
                records.append(
                    {
                        "path": relative,
                        "name": parsed_written["metadata"]["name"],
                        "summary": parsed_written["metadata"]["summary"],
                        "tags": parsed_written["metadata"]["tags"],
                        "updated": parsed_written["updated"],
                    }
                )
            records.sort(key=lambda record: record["path"])
            _write_index(root, records)
        except Exception as error:
            try:
                if old is None:
                    resolved.unlink(missing_ok=True)
                else:
                    _atomic_write(resolved, old.decode("utf-8"))
                if old_index is None:
                    (root / "index.yaml").unlink(missing_ok=True)
                else:
                    _atomic_write(root / "index.yaml", old_index.decode("utf-8"))
            except Exception as rollback:
                raise BrainError(
                    f"mutation failed: {error}; rollback failed: {rollback}"
                ) from error
            raise
    return content


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    terms = list(dict.fromkeys(query.lower().split()))
    if not terms:
        raise BrainError("query must not be empty")
    if limit < 0:
        raise BrainError("limit must not be negative")
    if limit == 0:
        return []
    records = ensure_fresh_index()
    root = brain_root()
    valid = {r["path"] for r in records}
    body_terms: dict[str, set[str]] = {}
    cmd = ["rg", "--json", "-i", "-F"]
    for term in terms:
        cmd.extend(["-e", term])
    cmd.extend(["--", str(root)])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise BrainError(f"ripgrep error: {proc.stderr.strip()}")
    import json

    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        p = obj.get("data", {}).get("path", {}).get("text", "")
        try:
            rel = Path(p).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in valid:
            try:
                parsed = parse_entry((root / rel).read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                parsed = None
            if parsed:
                body = parsed["body"]
                matched = {t for t in terms if t in body.lower()}
                if matched:
                    body_terms.setdefault(rel, set()).update(matched)
    hits = []
    for r in records:
        fields = {
            "name": r["name"].lower(),
            "tags": " ".join(r["tags"]).lower(),
            "summary": r["summary"].lower(),
        }
        score = sum(
            w * sum(t in fields[k] for t in terms)
            for k, w in (("name", 3), ("tags", 2), ("summary", 1))
        )
        score += len(body_terms.get(r["path"], set()))
        if not score:
            continue
        age = max(
            0.0,
            (datetime.now(UTC) - _timestamp(r["updated"]).replace(tzinfo=UTC)).total_seconds()
            / 86400,
        )
        hit = dict(r, score=score + 2.0 * math.exp(-age / 30.0), excerpt=None)
        if r["path"] in body_terms:
            try:
                parsed = parse_entry((root / r["path"]).read_text(encoding="utf-8"))
                body = parsed["body"] if parsed else ""
                hit["excerpt"] = next(
                    (
                        line.strip()
                        for line in body.splitlines()
                        if any(t in line.lower() for t in body_terms[r["path"]])
                    ),
                    "",
                )
            except OSError:
                pass
        hits.append(hit)
    hits.sort(key=lambda x: (x["score"], x["updated"]), reverse=True)
    return hits[: min(limit, 50)]
