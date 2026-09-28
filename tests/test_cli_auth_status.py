from unittest.mock import patch

from archie_cli.cli import main
from click.testing import CliRunner


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return [
            {
                "provider": "zeta",
                "auth_type": "static",
                "state": "configured",
                "expires_at": None,
            },
            {
                "provider": "google",
                "auth_type": "oauth",
                "state": "valid",
                "expires_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "provider": "alpha",
                "auth_type": "static",
                "state": "missing",
                "expires_at": None,
            },
            {
                "provider": "slack",
                "auth_type": "oauth",
                "state": "expired",
                "expires_at": "2025-01-01T00:00:00+00:00",
            },
        ]


@patch("archie_cli.auth.httpx.get", return_value=Response())
def test_auth_status_groups_and_sorts_credentials(mock_get):
    result = CliRunner().invoke(main, ["auth", "status"], color=False)

    assert result.exit_code == 0
    assert "OAuth credentials" in result.output
    assert "Static credentials" in result.output
    assert result.output.index("google") < result.output.index("slack")
    assert result.output.index("alpha") < result.output.index("zeta")
    assert result.output.index("OAuth credentials") < result.output.index("Static credentials")
    assert "2026-01-01 00:00:00 UTC" in result.output
    assert "ago" in result.output or "in" in result.output
    assert "Expires" not in result.output.split("Static credentials", 1)[1]
    mock_get.assert_called_once()
