"""Tests for canonical connect/status events and git branch discovery."""

from archie_shared.events import Handshake, SessionStatus, decode_event, encode_event


def test_handshake_contains_only_protocol_and_session_metadata():
    event = Handshake(
        id="01J00000000000000000000001",
        protocol_version=2,
        session_id="abc",
    )
    restored = decode_event(encode_event(event))
    assert restored == event
    assert not hasattr(restored, "model_key")


def test_session_status_round_trip():
    event = SessionStatus(
        id="01J00000000000000000000001", model_key="model-key", git_branch="feature/abc"
    )
    restored = decode_event(encode_event(event))
    assert isinstance(restored, SessionStatus)
    assert restored.model_key == "model-key"
    assert restored.git_branch == "feature/abc"


def test_session_status_is_live_only():
    event = SessionStatus(id="01J00000000000000000000001", model_key="m", git_branch="main")
    raw = encode_event(event)
    try:
        decode_event(raw, persisted=True)
    except Exception:
        pass
    else:
        raise AssertionError("live-only status event was accepted as persisted")


class TestReadGitBranch:
    """Tests for _read_git_branch helper."""

    def test_normal_branch(self, tmp_path, monkeypatch):
        from archie_agent.app import _read_git_branch

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/feature/my-branch\n")

        monkeypatch.setattr("archie_agent.app._WORKSPACE", str(tmp_path))
        assert _read_git_branch() == "feature/my-branch"

    def test_detached_head(self, tmp_path, monkeypatch):
        from archie_agent.app import _read_git_branch

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("a1b2c3d4e5f6789012345678901234567890abcd\n")

        monkeypatch.setattr("archie_agent.app._WORKSPACE", str(tmp_path))
        assert _read_git_branch() == "a1b2c3d4"

    def test_missing_git_dir(self, tmp_path, monkeypatch):
        from archie_agent.app import _read_git_branch

        monkeypatch.setattr("archie_agent.app._WORKSPACE", str(tmp_path))
        assert _read_git_branch() == "—"

    def test_empty_head_file(self, tmp_path, monkeypatch):
        from archie_agent.app import _read_git_branch

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("")

        monkeypatch.setattr("archie_agent.app._WORKSPACE", str(tmp_path))
        assert _read_git_branch() == "—"

    def test_permission_denied(self, tmp_path, monkeypatch):
        from archie_agent.app import _read_git_branch

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        head = git_dir / "HEAD"
        head.write_text("ref: refs/heads/main\n")
        head.chmod(0o000)

        monkeypatch.setattr("archie_agent.app._WORKSPACE", str(tmp_path))
        result = _read_git_branch()
        head.chmod(0o644)
        assert result == "—"
