"""Tests for archie ls and archie serve CLI commands (orchestrator integration)."""

from unittest.mock import MagicMock, patch

import msgspec
from archie_cli.cli import ls_cmd, main
from archie_shared.session import SessionDescriptor
from click.testing import CliRunner

# --- Helpers ---


def _make_sessions(*args) -> list[SessionDescriptor]:
    """Build a list of SessionDescriptors from (session_id, port, status) tuples."""
    return [
        SessionDescriptor(
            session_id=sid,
            container_name=f"archie-{sid}",
            port=port,
            raw_docker_status=status,
        )
        for sid, port, status in args
    ]


def _http_response(sessions: list[SessionDescriptor], status_code: int = 200) -> MagicMock:
    """Build a mock httpx.Response from a list of SessionDescriptors."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.content = msgspec.json.encode(sessions)
    mock.raise_for_status = MagicMock()
    return mock


# --- archie ls ---


def test_ls_displays_sessions(monkeypatch, tmp_path):
    """archie ls with HTTP response → correct table output."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    sessions = _make_sessions(
        ("myproject-01abc12345", 32771, "Up 3 hours"),
        ("otherapp-01def67890", None, "Up 10 minutes"),
    )
    mock_response = _http_response(sessions)

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        mock_httpx.ConnectError = __import__("httpx").ConnectError
        runner = CliRunner()
        result = runner.invoke(ls_cmd)

    assert result.exit_code == 0
    assert "myproject-01abc12345" in result.output
    assert "Up 3 hours" in result.output
    assert "32771" in result.output
    assert "otherapp-01def67890" in result.output
    assert "Up 10 minutes" in result.output
    # No port for second session
    assert "-" in result.output


def test_ls_no_sessions(monkeypatch, tmp_path):
    """archie ls with empty response → 'No running sessions.' message."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    mock_response = _http_response([])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        mock_httpx.ConnectError = __import__("httpx").ConnectError
        runner = CliRunner()
        result = runner.invoke(ls_cmd)

    assert result.exit_code == 0
    assert "No running sessions." in result.output


def test_ls_orchestrator_unreachable(monkeypatch, tmp_path):
    """archie ls when orchestrator unreachable → error with 'archie serve', exit non-zero."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.side_effect = real_httpx.ConnectError("Connection refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(ls_cmd)

    assert result.exit_code != 0
    assert "archie serve" in result.output


def test_ls_orchestrator_http_error(monkeypatch, tmp_path):
    """archie ls when orchestrator returns 5xx → user-friendly error, exit non-zero."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    import httpx as real_httpx

    # Build a mock response that raise_for_status will raise HTTPStatusError from
    mock_response = MagicMock()
    mock_response.status_code = 500

    def raise_status():
        raise real_httpx.HTTPStatusError(
            "Internal Server Error",
            request=MagicMock(),
            response=mock_response,
        )

    mock_response.raise_for_status = raise_status

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(ls_cmd)

    assert result.exit_code != 0
    assert "500" in result.output


def test_ls_no_docker_subprocess(monkeypatch, tmp_path):
    """archie ls does not spawn docker subprocess — only uses HTTP."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    mock_response = _http_response([])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = mock_response
        mock_httpx.ConnectError = __import__("httpx").ConnectError
        with patch("archie_cli.cli.subprocess.run") as mock_subprocess:
            runner = CliRunner()
            runner.invoke(ls_cmd)
            # subprocess.run must not have been called with docker ps
            for call in mock_subprocess.call_args_list:
                args = call[0][0] if call[0] else []
                assert not (len(args) >= 2 and args[0] == "docker" and args[1] == "ps"), (
                    "archie ls should not call docker ps directly"
                )


# --- archie serve ---


def test_serve_invokes_uvicorn_with_config(monkeypatch, tmp_path):
    """archie serve reads config and passes host/port to uvicorn.run."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("orchestrator:\n  port: 9900\n")

    with patch("archie_cli.cli.uvicorn") as mock_uvicorn:
        runner = CliRunner()
        runner.invoke(main, ["serve"])

    mock_uvicorn.run.assert_called_once_with(
        "archie_orchestrator.app:app",
        host="127.0.0.1",
        port=9900,
    )


def test_serve_uses_default_port_when_no_config(monkeypatch, tmp_path):
    """archie serve defaults to port 7600 when no orchestrator config."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    with patch("archie_cli.cli.uvicorn") as mock_uvicorn:
        runner = CliRunner()
        runner.invoke(main, ["serve"])

    mock_uvicorn.run.assert_called_once_with(
        "archie_orchestrator.app:app",
        host="127.0.0.1",
        port=7600,
    )
