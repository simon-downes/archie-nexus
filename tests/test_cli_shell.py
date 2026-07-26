"""Tests for archie shell CLI command (orchestrator resolution + direct exec)."""

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
) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=container_name,
        port=32771,
        raw_docker_status="Up",
    )


def test_shell_resolves_and_execs(monkeypatch, tmp_path):
    """archie shell fetches sessions from orchestrator and runs docker exec."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    session = _make_session()
    sessions_resp = _sessions_response([session])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            runner = CliRunner()
            runner.invoke(main, ["shell", "myproject"])

    # Confirm docker exec was called with the correct container
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "docker"
    assert cmd[1] == "exec"
    assert "archie-myproject-01abc12345" in cmd
    assert "bash" in cmd


def test_shell_no_sessions(monkeypatch, tmp_path):
    """archie shell with no running sessions → error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = _sessions_response([])
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["shell"])

    assert result.exit_code != 0
    assert "No running sessions" in result.output


def test_shell_prefix_matching(monkeypatch, tmp_path):
    """archie shell prefix matches to the correct session."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    sessions = [
        _make_session("myproject-01abc12345", "archie-myproject-01abc12345"),
        _make_session("otherapp-01def67890", "archie-otherapp-01def67890"),
    ]
    sessions_resp = _sessions_response(sessions)

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        with patch("archie_cli.cli.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            runner = CliRunner()
            runner.invoke(main, ["shell", "other"])

    cmd = mock_run.call_args[0][0]
    assert "archie-otherapp-01def67890" in cmd


def test_shell_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie shell when orchestrator unreachable → error with 'archie serve'."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["shell"])

    assert result.exit_code != 0
    assert "archie serve" in result.output
