import json
from unittest.mock import MagicMock, patch

import httpx
from archie_cli.auth import _response_error, _set_static_from_login, login
from click.testing import CliRunner


def _response():
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


def test_response_error_preserves_safe_orchestrator_detail():
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {
        "error": "OAuth provider 'google' requires a configured client_id"
    }
    assert _response_error(response) == "OAuth provider 'google' requires a configured client_id"


def test_response_error_falls_back_to_status():
    response = MagicMock()
    response.status_code = 502
    response.json.side_effect = ValueError
    assert _response_error(response) == "orchestrator returned HTTP 502"


def test_oauth_login_surfaces_orchestrator_configuration_error():
    request = httpx.Request("POST", "http://test/auth/login/google")
    response = httpx.Response(
        400,
        request=request,
        json={"error": "OAuth provider 'google' requires a configured client_id"},
    )
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch(
            "archie_cli.auth.httpx.post",
            side_effect=httpx.HTTPStatusError("bad", request=request, response=response),
        ),
    ):
        result = CliRunner().invoke(login, ["google"])
    assert result.exit_code != 0
    assert "requires a configured client_id" in result.output
    assert "Could not start orchestrator OAuth login" not in result.output


def test_github_login_uses_gh_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    response = _response()
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://127.0.0.1:7600"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("github")
    put.assert_called_once_with(
        "http://127.0.0.1:7600/auth/credential/github",
        json={"token": "github-secret"},
        timeout=10,
    )


def test_scalr_login_uses_environment(monkeypatch):
    monkeypatch.setenv("SCALR_HOSTNAME", "scalr.example")
    monkeypatch.setenv("SCALR_TOKEN", "scalr-secret")
    monkeypatch.setenv("SCALR_ACCOUNT", "account-1")
    response = _response()
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("scalr")
    assert put.call_args.kwargs["json"] == {
        "hostname": "scalr.example",
        "token": "scalr-secret",
        "account": "account-1",
    }


def test_static_login_passes_json_stdin(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin",
        MagicMock(
            isatty=lambda: False,
            read=lambda: json.dumps(
                {"email": "a@example.com", "token": "secret", "cloud_id": "cloud"}
            ),
        ),
    )
    response = _response()
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("jira")
    assert put.call_args.kwargs["json"]["cloud_id"] == "cloud"


def test_static_login_prompts_when_stdin_is_empty(monkeypatch):
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    response = _response()
    values = iter(["user@example.com", "secret", "cloud"])
    with (
        patch("archie_cli.auth.click.prompt", side_effect=lambda *args, **kwargs: next(values)),
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("jira")
    assert put.call_args.kwargs["json"] == {
        "email": "user@example.com",
        "token": "secret",
        "cloud_id": "cloud",
    }
