"""Tests for profile-based CLI addressing (archie start/stop/attach/ls with profiles)."""

from unittest.mock import MagicMock, patch

import msgspec
import pytest
from archie_cli.cli import main
from archie_shared.session import SessionDescriptor
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_response(sessions: list[SessionDescriptor]) -> MagicMock:
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


def _config_with_profile(tmp_path, profile_name: str, host: str, port: int = 7600) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"orchestrator:\n  profiles:\n    {profile_name}:\n      host: {host}\n      port: {port}\n"
    )
    return str(tmp_path)


# ---------------------------------------------------------------------------
# _resolve_target: profile/value parsing
# ---------------------------------------------------------------------------


def test_resolve_target_plain_value_uses_default_profile(monkeypatch, tmp_path):
    """'myproject' (no slash) → default profile."""
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = load_nexus_config()
    profile, value = _resolve_target("myproject", config)
    assert value == "myproject"
    assert profile.host == "127.0.0.1"
    assert profile.port == 7600


def test_resolve_target_profile_prefix(monkeypatch, tmp_path):
    """'gpu-box/myproject' → profile gpu-box, workspace myproject."""
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv(
        "ARCHIE_HOME_DIR", _config_with_profile(tmp_path, "gpu-box", "10.0.0.1", 7601)
    )
    config = load_nexus_config()
    profile, value = _resolve_target("gpu-box/myproject", config)
    assert value == "myproject"
    assert profile.host == "10.0.0.1"
    assert profile.port == 7601


def test_resolve_target_unknown_profile_raises(monkeypatch, tmp_path):
    """Unknown profile in 'profile/value' → ClickException."""
    import click
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = load_nexus_config()
    with pytest.raises(click.ClickException, match="Unknown profile"):
        _resolve_target("nonexistent/myproject", config)


def test_resolve_target_archie_host_override(monkeypatch, tmp_path):
    """ARCHIE_HOST env var overrides all profile resolution."""
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "192.168.1.100:8888")
    config = load_nexus_config()
    profile, value = _resolve_target("myproject", config)
    assert profile.host == "192.168.1.100"
    assert profile.port == 8888
    assert value == "myproject"


def test_resolve_target_archie_host_default_port(monkeypatch, tmp_path):
    """ARCHIE_HOST without port uses default port 7600."""
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "192.168.1.50")
    config = load_nexus_config()
    profile, _ = _resolve_target("myproject", config)
    assert profile.host == "192.168.1.50"
    assert profile.port == 7600


# ---------------------------------------------------------------------------
# archie start with profile prefix
# ---------------------------------------------------------------------------


def test_start_with_profile_prefix_uses_correct_url(monkeypatch, tmp_path):
    """archie start gpu-box/myproject POSTs to the gpu-box orchestrator URL."""
    import httpx as real_httpx

    monkeypatch.setenv(
        "ARCHIE_HOME_DIR", _config_with_profile(tmp_path, "gpu-box", "10.0.0.1", 7601)
    )

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
            result = runner.invoke(main, ["start", "gpu-box/myproject"])

    assert result.exit_code == 0, result.output
    call_args = mock_httpx.post.call_args
    assert "10.0.0.1:7601" in call_args[0][0]
    assert call_args[1]["json"]["workspace"] == "myproject"


# ---------------------------------------------------------------------------
# archie ls — multi-profile
# ---------------------------------------------------------------------------


def test_ls_no_profile_queries_default(monkeypatch, tmp_path):
    """archie ls with no arg queries the default profile."""
    import httpx as real_httpx

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    sessions_resp = _session_response([_make_session()])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    mock_httpx.get.assert_called()
    url = mock_httpx.get.call_args[0][0]
    assert "127.0.0.1:7600" in url


def test_ls_named_profile_queries_that_profile(monkeypatch, tmp_path):
    """archie ls gpu-box queries the gpu-box orchestrator."""
    import httpx as real_httpx

    monkeypatch.setenv(
        "ARCHIE_HOME_DIR", _config_with_profile(tmp_path, "gpu-box", "10.0.0.1", 7601)
    )
    sessions_resp = _session_response([_make_session()])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["ls", "gpu-box"])

    assert result.exit_code == 0
    url = mock_httpx.get.call_args[0][0]
    assert "10.0.0.1:7601" in url


def test_ls_unknown_profile_raises_error(monkeypatch, tmp_path):
    """archie ls with unknown profile → ClickException."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["ls", "nonexistent"])
    assert result.exit_code != 0
    assert "Unknown profile" in result.output


def test_ls_unreachable_profile_shown_gracefully(monkeypatch, tmp_path):
    """archie ls with unreachable profile → error shown but exit 0."""
    import httpx as real_httpx

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.side_effect = real_httpx.ConnectError("refused")
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        mock_httpx.HTTPError = real_httpx.HTTPError
        runner = CliRunner()
        result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    assert "Unreachable" in result.output


def test_ls_all_profiles_multi_host(monkeypatch, tmp_path):
    """archie ls with two profiles queries both and labels output."""
    import httpx as real_httpx

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "orchestrator:\n"
        "  profiles:\n"
        "    default:\n"
        "      host: 127.0.0.1\n"
        "      port: 7600\n"
        "    gpu-box:\n"
        "      host: 10.0.0.1\n"
        "      port: 7601\n"
    )
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    sessions_resp = _session_response([_make_session()])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    # Should have queried both profiles
    assert mock_httpx.get.call_count == 2
    # Labels shown
    assert "[default]" in result.output
    assert "[gpu-box]" in result.output


# ---------------------------------------------------------------------------
# ARCHIE_HOST env var
# ---------------------------------------------------------------------------


def test_archie_host_overrides_profile_in_ls(monkeypatch, tmp_path):
    """ARCHIE_HOST env var makes ls query the override address."""
    import httpx as real_httpx

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "192.168.1.99:9999")

    sessions_resp = _session_response([_make_session()])

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    url = mock_httpx.get.call_args[0][0]
    assert "192.168.1.99:9999" in url


def test_resolve_target_archie_host_strips_profile_prefix(monkeypatch, tmp_path):
    """ARCHIE_HOST set + 'profile/value' arg → host from env, value stripped of prefix."""
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "10.0.0.1:9000")
    config = load_nexus_config()
    profile, value = _resolve_target("gpu-box/myproject", config)
    # Host comes from ARCHIE_HOST, value is the remainder after '/'
    assert profile.host == "10.0.0.1"
    assert profile.port == 9000
    assert value == "myproject"  # NOT "gpu-box/myproject"


def test_resolve_target_archie_host_malformed_port_raises(monkeypatch, tmp_path):
    """ARCHIE_HOST with non-integer port → ClickException with clear message."""
    import click
    from archie_cli.cli import _resolve_target
    from archie_shared.schemas import load_nexus_config

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "10.0.0.1:notaport")
    config = load_nexus_config()
    with pytest.raises(click.ClickException, match="port must be an integer"):
        _resolve_target("myproject", config)
