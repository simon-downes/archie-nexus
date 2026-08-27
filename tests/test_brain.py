from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from archie_agent.exec.tools.fs import edit, write
from archie_shared.brain import (
    _lock_path,
    brain_root,
    parse_entry,
    rebuild_index,
    search,
)


def _entry(name: str, updated: str = "2025-01-01T00:00:00+00:00", body: str = "") -> str:
    return f"---\nname: {name}\nsummary: summary {name}\ntags: [tag]\nupdated: '{updated}'\n---\n{body}"


def test_default_root_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("ARCHIE_BRAIN_DIR", raising=False)
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path / "home"))
    assert brain_root() == (tmp_path / "home" / "brain").resolve()


def test_root_resolution_and_timestamp_precision(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", "custom")
    assert brain_root() == (tmp_path / "home" / "custom").resolve()
    assert _lock_path(brain_root()) == tmp_path / "home" / ".custom.brain.lock"
    parsed = parse_entry(_entry("one"))
    assert parsed["updated"] == "2025-01-01T00:00:00.000000+00:00"


@pytest.mark.parametrize(
    "content",
    [
        "plain text",
        "---\\nname: bad\\nsummary: [broken\\n---\\nbody",
        "---\\nname: missing\\n---\\nbody",
    ],
)
def test_malformed_entries_are_excluded(monkeypatch, tmp_path, content):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "bad.md").write_text(content)
    assert rebuild_index() == []


def test_arbitrary_nested_reserved_names_are_indexed(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    for relative in ("archive/BRAIN.md", "notes/index.yaml", "_draft.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_entry(relative))
    records = rebuild_index()
    assert {record["path"] for record in records} == {
        "archive/BRAIN.md",
        "notes/index.yaml",
        "_draft.md",
    }


def test_rebuild_excludes_root_infrastructure(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "BRAIN.md").write_text(_entry("guidance"))
    (tmp_path / "real.md").write_text(_entry("real"))
    assert [record["path"] for record in rebuild_index()] == ["real.md"]


def test_manual_index_cannot_expose_root_infrastructure(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "BRAIN.md").write_text(_entry("guidance"))
    (tmp_path / "index.yaml").write_text(
        "entries:\n- path: BRAIN.md\n  name: guidance\n  summary: bad\n  tags: []\n  updated: '2025-01-01T00:00:00+00:00'\n"
    )
    assert search("guidance") == []


def test_binary_file_can_be_overwritten(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    path = tmp_path / "binary.dat"
    path.write_bytes(b"\\x00\\xff")
    from archie_shared.brain import mutate

    mutate(path, "ordinary text")
    assert path.read_text() == "ordinary text"


@pytest.mark.asyncio
async def test_write_timestamps_indexes_and_searches(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    await write(
        str(tmp_path / "nested" / "entry.md"),
        _entry("written", updated="2000-01-01T00:00:00+00:00", body="write body"),
    )
    hits = search("write body")
    assert hits[0]["path"] == "nested/entry.md"
    assert "2000" not in (tmp_path / "nested" / "entry.md").read_text()


@pytest.mark.asyncio
async def test_edit_timestamps_indexes_and_searches(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    path = tmp_path / "entry.md"
    path.write_text(_entry("entry", body="old body"))
    rebuild_index()
    await edit(str(path), "old body", "new body")
    assert search("new body")[0]["path"] == "entry.md"
    assert search("old") == []


@pytest.mark.asyncio
async def test_invalid_existing_edit_preserves_file_and_index(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    path = tmp_path / "entry.md"
    original = _entry("entry", body="keep")
    path.write_text(original)
    rebuild_index()
    from archie_agent.exec.tools import EditError

    with pytest.raises(EditError):
        await edit(str(path), "name: entry", "name: ")
    assert path.read_text() == original
    assert search("keep")[0]["path"] == "entry.md"


def test_missing_and_malformed_index_rebuild(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "entry.md").write_text(_entry("entry"))
    (tmp_path / "index.yaml").write_text("not: [valid")
    assert search("entry")[0]["path"] == "entry.md"


def test_stale_index_detects_changed_and_deleted_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    path = tmp_path / "entry.md"
    path.write_text(_entry("old"))
    rebuild_index()
    old = time.time() - 2 * 86400
    os.utime(tmp_path / "index.yaml", (old, old))
    path.write_text(_entry("changed"))
    assert search("changed")[0]["name"] == "changed"
    path.unlink()
    assert search("changed") == []


def test_search_matches_body_only_and_uses_literal_terms(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    path = tmp_path / "entry.md"
    path.write_text(_entry("entry", body="literal [punctuation] body"))
    rebuild_index()
    hits = search("[punctuation]")
    assert hits[0]["path"] == "entry.md"
    assert "literal" in hits[0]["excerpt"]


def test_concurrent_rebuilds_share_adjacent_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "entry.md").write_text(_entry("entry"))
    script = "from archie_shared.brain import rebuild_index; rebuild_index()"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
    processes = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(2)]
    assert all(process.wait(timeout=10) == 0 for process in processes)
    assert (tmp_path.parent / f".{tmp_path.name}.brain.lock").exists()


def test_search_ranking_or_terms_and_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "name.md").write_text(_entry("alpha", body=""))
    (tmp_path / "summary.md").write_text(_entry("other", body="alpha"))
    rebuild_index()
    hits = search("alpha beta", limit=50)
    assert hits[0]["path"] == "name.md"
    assert len(search("alpha", limit=0)) == 0
    assert len(search("alpha", limit=500)) <= 50


def test_zero_limit_skips_search(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    assert search("anything", limit=0) == []
    with pytest.raises(Exception, match="negative"):
        search("anything", limit=-1)
