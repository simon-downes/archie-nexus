"""Tests for git branch wire protocol and StatusUpdated event."""

import json

from archie_shared.events import (
    SessionInfo,
    StatusUpdated,
    deserialize_event,
    serialize_event,
)


class TestSessionInfoGitBranch:
    """Tests for SessionInfo.git_branch field."""

    def test_to_json_includes_git_branch(self):
        info = SessionInfo(
            protocol_version=1, model="test-model", session_id="abc", git_branch="main"
        )
        data = info.to_json()
        assert data["data"]["git_branch"] == "main"

    def test_from_json_with_git_branch(self):
        info = SessionInfo.from_json(
            {"protocol_version": 1, "model": "m", "session_id": "s", "git_branch": "feat/x"}
        )
        assert info.git_branch == "feat/x"

    def test_from_json_missing_git_branch_defaults(self):
        """Backward compatibility: missing git_branch defaults to '—'."""
        info = SessionInfo.from_json({"protocol_version": 1, "model": "m", "session_id": "s"})
        assert info.git_branch == "—"

    def test_round_trip(self):
        info = SessionInfo(protocol_version=1, model="m", session_id="s", git_branch="develop")
        raw = serialize_event(info)
        restored = deserialize_event(raw)
        assert isinstance(restored, SessionInfo)
        assert restored.git_branch == "develop"

    def test_default_value(self):
        info = SessionInfo(protocol_version=1, model="m", session_id="s")
        assert info.git_branch == "—"


class TestStatusUpdated:
    """Tests for StatusUpdated event."""

    def test_to_json(self):
        event = StatusUpdated(git_branch="main")
        data = event.to_json()
        assert data == {"type": "status_updated", "data": {"git_branch": "main"}}

    def test_from_json(self):
        event = StatusUpdated.from_json({"git_branch": "feature/abc"})
        assert event.git_branch == "feature/abc"

    def test_from_json_missing_git_branch_defaults(self):
        event = StatusUpdated.from_json({})
        assert event.git_branch == "—"

    def test_round_trip(self):
        event = StatusUpdated(git_branch="hotfix/123")
        raw = serialize_event(event)
        restored = deserialize_event(raw)
        assert isinstance(restored, StatusUpdated)
        assert restored.git_branch == "hotfix/123"

    def test_deserialized_without_turn_index(self):
        """StatusUpdated is session-level — no turn_index in wire format."""
        raw = json.dumps({"type": "status_updated", "data": {"git_branch": "main"}})
        event = deserialize_event(raw)
        assert isinstance(event, StatusUpdated)
        assert not hasattr(event, "turn_index")


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
        # Restore permissions for cleanup
        head.chmod(0o644)
        assert result == "—"
