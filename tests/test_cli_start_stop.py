"""Tests for archie start and archie stop CLI commands (orchestrator API path)."""

from unittest.mock import MagicMock, patch

import msgspec
from archie_cli.cli import main
from archie_shared.session import SessionDescriptor
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_response(
    session_id: str = "myproject-01abc12345",
    port: int = 32771,
    status: str = "Up",
) -> MagicMock:
    """Build a mock httpx.Response containing a single SessionDescriptor."""
    descriptor = SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=port,
        raw_docker_status=status,
    )
    mock = MagicMock()
    mock.status_code = 200
    mock.content = msgspec.json.encode(descriptor)
    mock.json.return_value = {"session_id": session_id, "port": port}
    mock.raise_for_status = MagicMock()
    return mock


def _sessions_list_response(sessions: list[SessionDescriptor]) -> MagicMock:
    """Build a mock httpx.Response for GET /sessions."""
    mock = MagicMock()
    mock.status_code = 200
    mock.content = msgspec.json.encode(sessions)
    mock.raise_for_status = MagicMock()
    return mock


def _error_response(status_code: int, error: str) -> MagicMock:
    """Build a mock httpx.Response with an error payload."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"error": error}
    mock.raise_for_status = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# archie start
# ---------------------------------------------------------------------------


def test_start_detach_prints_session_info(monkeypatch, tmp_path):
    """archie start --detach prints session ID, container, and agent URL."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.return_value = _session_response()
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["start", "--detach", "--workspace", "myproject"])

    assert result.exit_code == 0, result.output
    assert "myproject-01abc12345" in result.output
    assert "32771" in result.output


def test_start_sends_correct_workspace(monkeypatch, tmp_path):
    """archie start --workspace sends the given workspace name in POST body."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.return_value = _session_response()
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        runner.invoke(main, ["start", "--detach", "--workspace", "my-app"])

    mock_httpx.post.assert_called_once()
    call_kwargs = mock_httpx.post.call_args
    assert call_kwargs.kwargs["json"] == {"workspace": "my-app"}


def test_start_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie start when orchestrator unreachable → error with 'archie serve'."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["start", "--detach", "--workspace", "myproject"])

    assert result.exit_code != 0
    assert "archie serve" in result.output


def test_start_workspace_not_found_400(monkeypatch, tmp_path):
    """archie start when orchestrator returns 400 → workspace error displayed."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.return_value = _error_response(400, "Workspace 'bad' not found")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["start", "--detach", "--workspace", "bad"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_start_launches_tui_when_not_detached(monkeypatch, tmp_path):
    """archie start without --detach launches the TUI."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.return_value = _session_response()
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_tui:
            mock_tui.return_value.run = MagicMock()
            runner = CliRunner()
            runner.invoke(main, ["start", "--workspace", "myproject"])

    mock_tui.assert_called_once_with(
        ws_url="ws://127.0.0.1:7600/sessions/myproject-01abc12345/stream",
        api_url="http://127.0.0.1:7600/sessions/myproject-01abc12345",
        container_name="archie-myproject-01abc12345",
    )
    mock_tui.return_value.run.assert_called_once()


# ---------------------------------------------------------------------------
# archie stop
# ---------------------------------------------------------------------------


def _make_sessions(*args) -> list[SessionDescriptor]:
    return [
        SessionDescriptor(
            session_id=sid,
            container_name=f"archie-{sid}",
            port=port,
            raw_docker_status=status,
        )
        for sid, port, status in args
    ]


def test_stop_sends_delete_for_matching_session(monkeypatch, tmp_path):
    """archie stop with prefix → resolves and sends DELETE /sessions/{id}."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    sessions = _make_sessions(("myproject-01abc12345", 32771, "Up"))
    sessions_resp = _sessions_list_response(sessions)
    stop_resp = MagicMock()
    stop_resp.status_code = 200
    stop_resp.json.return_value = {"stopped": "myproject-01abc12345"}

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.delete.return_value = stop_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "myproject"])

    assert result.exit_code == 0
    assert "myproject-01abc12345" in result.output
    mock_httpx.delete.assert_called_once()
    assert "myproject-01abc12345" in mock_httpx.delete.call_args[0][0]


def test_stop_no_sessions(monkeypatch, tmp_path):
    """archie stop with no sessions → clear error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_list_response([])
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "No running sessions" in result.output


def test_stop_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie stop when orchestrator unreachable → error with 'archie serve'."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["stop"])

    assert result.exit_code != 0
    assert "archie serve" in result.output
