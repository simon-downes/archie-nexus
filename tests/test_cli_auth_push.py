"""The legacy whole-file credential push command is absent."""

from archie_cli.cli import main
from click.testing import CliRunner


def test_auth_help_excludes_removed_commands():
    result = CliRunner().invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    for command in ("bedrock", "set", "login-local", "push"):
        assert command not in result.output
    assert "login" in result.output
    assert "refresh" in result.output
    assert "status" in result.output
