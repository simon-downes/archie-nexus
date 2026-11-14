"""Tests for archie attach CLI command (orchestrator proxy path)."""

from unittest.mock import MagicMock, patch

import msgspec
from archie_cli.cli import main
from archie_cli.tui.client import HttpOrchestrator, ProfileInfo
from archie_shared.session import SessionDescriptor, split_id
from click.testing import CliRunner


def _sessions_response(sessions: list[SessionDescriptor]) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.content = msgspec.json.encode(sessions)
    mock.raise_for_status = MagicMock()
    return mock


def _make_session(
    session_id: str = "myproject-01abc12345", status: str = "idle"
) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        workspace=split_id(session_id)[0],
        backend="native",
        name=session_id,
        status=status,
        started_at="2026-07-01T10:00:00+00:00",
    )


LOCAL = ProfileInfo(label="local", web_url="http://127.0.0.1:7600/", local=True)


def _assert_app(mock_app, *, focus, profile=LOCAL, base_url="http://127.0.0.1:7600"):
    """The TUI was opened once with an HttpOrchestrator for ``base_url`` (084 D19)."""
    mock_app.assert_called_once()
    (client,), kwargs = mock_app.call_args
    assert isinstance(client, HttpOrchestrator)
    assert client.base_url == base_url
    assert kwargs == {"profile": profile, "focus": focus}
    mock_app.return_value.run.assert_called_once()


def test_attach_opens_the_tui_on_the_matching_session(monkeypatch, tmp_path):
    """archie attach PREFIX opens the TUI focused on the session the prefix matches."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    session = _make_session()
    sessions_resp = _sessions_response([session])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            mock_app.return_value.run = MagicMock()
            runner = CliRunner()
            result = runner.invoke(main, ["attach", "myproject"])

    assert result.exit_code == 0, result.output
    _assert_app(mock_app, focus="myproject-01abc12345")


def test_attach_ambiguous_prefix_picks_by_number(monkeypatch, tmp_path):
    """Several matches still show the numbered picker; the choice is focused."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    sessions = [_make_session("myproject-01aaa"), _make_session("myproject-01bbb")]
    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_response(sessions)
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            mock_app.return_value.run = MagicMock()
            result = CliRunner().invoke(main, ["attach", "myproject"], input="2\n")

    assert result.exit_code == 0, result.output
    assert "Multiple sessions match" in result.output
    _assert_app(mock_app, focus="myproject-01bbb")


def test_attach_single_session_no_prefix(monkeypatch, tmp_path):
    """archie attach with no args opens the TUI with no focus (it shows the newest)."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    session = _make_session()
    sessions_resp = _sessions_response([session])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            mock_app.return_value.run = MagicMock()
            runner = CliRunner()
            result = runner.invoke(main, ["attach"])

    assert result.exit_code == 0, result.output
    _assert_app(mock_app, focus=None)


def test_attach_no_sessions(monkeypatch, tmp_path):
    """archie attach with no running sessions opens the TUI with no focus (084 D19)."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_response([])
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            mock_app.return_value.run = MagicMock()
            result = CliRunner().invoke(main, ["attach"])

    assert result.exit_code == 0, result.output
    _assert_app(mock_app, focus=None)


def test_attach_unknown_prefix_still_errors(monkeypatch, tmp_path):
    """A prefix that matches nothing is an error, as before."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_response([_make_session()])
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            result = CliRunner().invoke(main, ["attach", "other"])

    assert result.exit_code != 0
    assert "No session matching 'other'" in result.output
    mock_app.assert_not_called()


def test_attach_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie attach when orchestrator unreachable → error with 'archie serve'."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["attach"])

    assert result.exit_code != 0
    assert "archie serve" in result.output


def test_start_non_detach_opens_the_tui_on_the_new_session(monkeypatch, tmp_path):
    """archie start (non-detach) opens the TUI focused on the new session."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    descriptor = _make_session()
    post_resp = MagicMock()
    post_resp.status_code = 202
    post_resp.content = msgspec.json.encode(descriptor)
    post_resp.json.return_value = {}

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.return_value = post_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            mock_app.return_value.run = MagicMock()
            runner = CliRunner()
            result = runner.invoke(main, ["start", "myproject"])

    assert result.exit_code == 0, result.output
    _assert_app(mock_app, focus="myproject-01abc12345")
