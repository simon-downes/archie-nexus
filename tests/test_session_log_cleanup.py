from unittest.mock import patch

from archie_orchestrator.lifecycle import _remove_empty_session_log
from archie_shared.events import SessionStarted, UserMessage
from archie_shared.session.log import SessionLog


def _session_started() -> SessionStarted:
    return SessionStarted(
        id="started",
        schema_version=2,
        sent_at="2025-01-01T00:00:00Z",
        model_key="model",
    )


def test_remove_empty_session_log_removes_idle_session(tmp_path):
    path = tmp_path / "sessions" / "idle.jsonl"
    SessionLog(path).append(_session_started())

    with patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path):
        _remove_empty_session_log("idle")

    assert not path.exists()


def test_remove_empty_session_log_removes_shell_only_session(tmp_path):
    path = tmp_path / "sessions" / "shell-only.jsonl"
    SessionLog(path).append(_session_started())

    with patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path):
        _remove_empty_session_log("shell-only")

    assert not path.exists()


def test_remove_empty_session_log_preserves_prompt_session(tmp_path):
    path = tmp_path / "sessions" / "active.jsonl"
    log = SessionLog(path)
    log.append(_session_started())
    log.append(UserMessage(id="message", turn=1, scope=None, content="hello"))

    with patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path):
        _remove_empty_session_log("active")

    assert path.exists()


def test_remove_empty_session_log_preserves_unreadable_log(tmp_path):
    path = tmp_path / "sessions" / "broken.jsonl"
    path.parent.mkdir()
    path.write_text("not valid json\n")

    with patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path):
        _remove_empty_session_log("broken")

    assert path.exists()


def test_remove_empty_session_log_preserves_log_with_malformed_line(tmp_path):
    path = tmp_path / "sessions" / "partial.jsonl"
    SessionLog(path).append(_session_started())
    with path.open("a") as stream:
        stream.write("not valid json\n")

    with patch("archie_orchestrator.lifecycle.home_dir", return_value=tmp_path):
        _remove_empty_session_log("partial")

    assert path.exists()
