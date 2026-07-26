"""Tests for archie attach CLI command (orchestrator proxy path)."""

from unittest.mock import MagicMock, patch

import msgspec
from archie_cli.cli import main
from archie_shared.session import SessionDescriptor
from click.testing import CliRunner


def _sessions_response(sessions: list[SessionDescriptor]) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.content = msgspec.json.encode(sessions)
    mock.raise_for_status = MagicMock()
    return mock


def _make_session(
    session_id: str = "myproject-01abc12345",
    container_name: str = "archie-myproject-01abc12345",
    port: int = 32771,
) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=container_name,
        port=port,
        raw_docker_status="Up",
    )


def test_attach_constructs_correct_urls(monkeypatch, tmp_path):
    """archie attach constructs ws_url and api_url from orchestrator config."""
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
    mock_app.assert_called_once_with(
        ws_url="ws://127.0.0.1:7600/sessions/myproject-01abc12345/stream",
        api_url="http://127.0.0.1:7600/sessions/myproject-01abc12345",
        container_name="archie-myproject-01abc12345",
    )


def test_attach_single_session_no_prefix(monkeypatch, tmp_path):
    """archie attach with no args and one session attaches to it."""
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
    mock_app.assert_called_once()


def test_attach_no_sessions(monkeypatch, tmp_path):
    """archie attach with no running sessions → error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_response([])
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["attach"])

    assert result.exit_code != 0
    assert "No running sessions" in result.output


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


def test_start_non_detach_uses_proxy_urls(monkeypatch, tmp_path):
    """archie start (non-detach) constructs orchestrator proxy URLs for TUI."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    descriptor = _make_session()
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.content = msgspec.json.encode(descriptor)
    post_resp.json.return_value = {}

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.post.return_value = post_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.tui.app.ArchieApp") as mock_app:
            mock_app.return_value.run = MagicMock()
            runner = CliRunner()
            runner.invoke(main, ["start", "--workspace", "myproject"])

    mock_app.assert_called_once_with(
        ws_url="ws://127.0.0.1:7600/sessions/myproject-01abc12345/stream",
        api_url="http://127.0.0.1:7600/sessions/myproject-01abc12345",
        container_name="archie-myproject-01abc12345",
    )
