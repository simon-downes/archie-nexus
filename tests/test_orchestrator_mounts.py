"""Tests for configured host mount resolution."""

from pathlib import Path

import pytest
from archie_orchestrator.mounts import resolve_mounts


def test_resolve_literal_source_uses_container_home(tmp_path, monkeypatch):
    source = tmp_path / ".gitconfig"
    source.write_text("[user]\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_mounts(("~/.gitconfig",)) == (f"{source}:/home/archie/.gitconfig:rw",)


def test_resolve_wildcard_source_creates_sorted_separate_mounts(tmp_path, monkeypatch):
    (tmp_path / ".gitconfig.work").write_text("")
    (tmp_path / ".gitconfig").write_text("")
    (tmp_path / ".gitconfig.local").write_text("")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_mounts(("~/.gitconfig*",)) == tuple(
        f"{path}:/home/archie/{path.name}:rw" for path in sorted(tmp_path.glob(".gitconfig*"))
    )


def test_resolve_wildcard_pair_appends_basename_and_preserves_readonly(tmp_path, monkeypatch):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    private = ssh / "id_ed25519"
    public = ssh / "id_ed25519.pub"
    private.write_text("")
    public.write_text("")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_mounts((("~/.ssh/id_ed25519*", "~/.ssh:ro"),)) == (
        f"{private}:/home/archie/.ssh/id_ed25519:ro",
        f"{public}:/home/archie/.ssh/id_ed25519.pub:ro",
    )


def test_resolve_source_mode_suffix(tmp_path):
    source = tmp_path / "key"
    source.write_text("")

    assert resolve_mounts((f"{source}:ro",)) == (f"{source}:{source}:ro",)


def test_resolve_literal_pair_expands_home_destination(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_mounts(((str(source), "~/.config/source:ro"),)) == (
        f"{source}:/home/archie/.config/source:ro",
    )


def test_resolve_missing_source_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert resolve_mounts(("~/.gitconfig*",)) == ()


def test_resolve_rejects_invalid_mode(tmp_path):
    source = tmp_path / "source"
    source.write_text("")

    with pytest.raises(ValueError, match="mode"):
        resolve_mounts(((str(source), "/target:shared"),))


def test_resolve_rejects_empty_destination(tmp_path):
    source = tmp_path / "source"
    source.write_text("")

    with pytest.raises(ValueError, match="path must not be empty"):
        resolve_mounts(((str(source), ""),))


def test_resolve_rejects_relative_destination(tmp_path):
    source = tmp_path / "source"
    source.write_text("")

    with pytest.raises(ValueError, match="must be absolute"):
        resolve_mounts(((str(source), "relative/path"),))


def test_resolve_rejects_invalid_destination_for_missing_source(tmp_path):
    with pytest.raises(ValueError, match="must be absolute"):
        resolve_mounts(((str(tmp_path / "missing"), "relative/path"),))


def test_resolve_rejects_fixed_target_for_unmatched_glob(tmp_path):
    with pytest.raises(ValueError, match="fixed mount"):
        resolve_mounts(((str(tmp_path / "missing*"), "/workspace/output:ro"),))


def test_resolve_rejects_wildcard_directory_component(tmp_path):
    with pytest.raises(ValueError, match="final path component"):
        resolve_mounts(((str(tmp_path / "dir*" / "file"), "/target:ro"),))


def test_resolve_rejects_fixed_mount_target(tmp_path):
    source = tmp_path / "source"
    source.write_text("")

    with pytest.raises(ValueError, match="fixed mount"):
        resolve_mounts(((str(source), "/workspace/output:ro"),))


def test_resolve_rejects_recursive_glob(tmp_path):
    with pytest.raises(ValueError, match="Recursive"):
        resolve_mounts(((str(tmp_path / "**"), "/keys:ro"),))
