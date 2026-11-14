"""Tests for opening the TUI from the CLI."""

from unittest.mock import MagicMock, patch

from archie_cli.cli import main
from archie_cli.tui.client import HttpOrchestrator, ProfileInfo
from click.testing import CliRunner

LOCAL = ProfileInfo(label="local", web_url="http://127.0.0.1:7600/", local=True)


def _assert_app(mock_app, *, focus, profile=LOCAL, base_url="http://127.0.0.1:7600"):
    """The TUI was opened once with an HttpOrchestrator."""
    mock_app.assert_called_once()
    (client,), kwargs = mock_app.call_args
    assert isinstance(client, HttpOrchestrator)
    assert client.base_url == base_url
    assert kwargs == {"profile": profile, "focus": focus}
    mock_app.return_value.run.assert_called_once()


def test_bare_archie_opens_the_tui(monkeypatch, tmp_path):
    """archie without a subcommand opens the TUI with no forced session."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))

    with patch("archie_cli.tui.app.ArchieApp") as mock_app:
        mock_app.return_value.run = MagicMock()
        result = CliRunner().invoke(main, [])

    assert result.exit_code == 0, result.output
    _assert_app(mock_app, focus=None)


def test_bare_archie_uses_archie_host(monkeypatch, tmp_path):
    """ARCHIE_HOST selects the TUI's orchestrator without a CLI argument."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "example.test:7700")

    with patch("archie_cli.tui.app.ArchieApp") as mock_app:
        mock_app.return_value.run = MagicMock()
        result = CliRunner().invoke(main, [])

    assert result.exit_code == 0, result.output
    _assert_app(
        mock_app,
        focus=None,
        profile=ProfileInfo("example.test:7700", "http://example.test:7700/", False),
        base_url="http://example.test:7700",
    )


def test_removed_session_commands_are_not_registered():
    """Session selection and stopping are TUI actions, not top-level CLI commands."""
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0, result.output
    commands = result.output.split("Commands:", 1)[1]
    assert "attach" not in commands
    assert "\n  stop " not in commands
