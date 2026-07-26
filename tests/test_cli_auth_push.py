"""Tests for archie auth push CLI command."""

from unittest.mock import MagicMock, patch

from archie_cli.cli import main
from click.testing import CliRunner


def _config_with_profile(tmp_path, profile: str, host: str, port: int = 7600) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"orchestrator:\n"
        f"  profiles:\n"
        f"    {profile}:\n"
        f"      host: {host}\n"
        f"      port: {port}\n"
    )


def test_auth_push_sends_correct_content(monkeypatch, tmp_path):
    """auth push reads credentials.yaml and POSTs it to the profile."""
    import httpx as real_httpx

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    _config_with_profile(tmp_path, "gpu-box", "10.0.0.1", 7601)

    cred_file = tmp_path / "credentials.yaml"
    cred_content = b"bedrock:\n  aws_access_key_id: AKIA123\n"
    cred_file.write_bytes(cred_content)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("archie_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "push", "gpu-box"])

    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    call_args = mock_httpx.post.call_args
    assert "10.0.0.1:7601/credentials" in call_args[0][0]
    assert call_args[1]["content"] == cred_content


def test_auth_push_unknown_profile_errors(monkeypatch, tmp_path):
    """auth push with unknown profile → error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "push", "nonexistent"])
    assert result.exit_code != 0
    assert "Unknown profile" in result.output


def test_auth_push_no_local_credentials_errors(monkeypatch, tmp_path):
    """auth push with no local credentials.yaml → clear error."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    _config_with_profile(tmp_path, "gpu-box", "10.0.0.1")

    runner = CliRunner()
    result = runner.invoke(main, ["auth", "push", "gpu-box"])
    assert result.exit_code != 0
    assert "credentials" in result.output.lower()


def test_auth_push_orchestrator_unreachable_errors(monkeypatch, tmp_path):
    """auth push when orchestrator unreachable → clear error with URL."""
    import httpx as real_httpx

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    _config_with_profile(tmp_path, "gpu-box", "10.0.0.1")

    cred_file = tmp_path / "credentials.yaml"
    cred_file.write_bytes(b"key: value\n")

    with patch("archie_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "push", "gpu-box"])

    assert result.exit_code != 0
    assert "Cannot connect" in result.output


def test_auth_push_http_error_reported(monkeypatch, tmp_path):
    """auth push when orchestrator returns non-200 → error with status code."""
    import httpx as real_httpx

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    _config_with_profile(tmp_path, "gpu-box", "10.0.0.1")

    cred_file = tmp_path / "credentials.yaml"
    cred_file.write_bytes(b"key: value\n")

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch("archie_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = mock_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        runner = CliRunner()
        result = runner.invoke(main, ["auth", "push", "gpu-box"])

    assert result.exit_code != 0
    assert "500" in result.output
