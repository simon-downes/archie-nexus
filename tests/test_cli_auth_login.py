import json
from unittest.mock import MagicMock, patch

from archie_cli.auth import _set_static_from_login


def _response():
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


def test_github_login_uses_gh_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    response = _response()
    with patch("archie_cli.auth._orchestrator_url", return_value="http://127.0.0.1:7600"), patch(
        "archie_cli.auth.httpx.put", return_value=response
    ) as put:
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
    with patch("archie_cli.auth._orchestrator_url", return_value="http://test"), patch(
        "archie_cli.auth.httpx.put", return_value=response
    ) as put:
        _set_static_from_login("scalr")
    assert put.call_args.kwargs["json"] == {
        "hostname": "scalr.example",
        "token": "scalr-secret",
        "account": "account-1",
    }


def test_static_login_passes_json_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False, read=lambda: json.dumps({"email": "a@example.com", "token": "secret", "cloud_id": "cloud"})))
    response = _response()
    with patch("archie_cli.auth._orchestrator_url", return_value="http://test"), patch(
        "archie_cli.auth.httpx.put", return_value=response
    ) as put:
        _set_static_from_login("jira")
    assert put.call_args.kwargs["json"]["cloud_id"] == "cloud"


def test_static_login_prompts_when_stdin_is_empty(monkeypatch):
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    response = _response()
    values = iter(["user@example.com", "secret", "cloud"])
    with patch("archie_cli.auth.click.prompt", side_effect=lambda *args, **kwargs: next(values)), patch(
        "archie_cli.auth._orchestrator_url", return_value="http://test"
    ), patch("archie_cli.auth.httpx.put", return_value=response) as put:
        _set_static_from_login("jira")
    assert put.call_args.kwargs["json"] == {
        "email": "user@example.com",
        "token": "secret",
        "cloud_id": "cloud",
    }
