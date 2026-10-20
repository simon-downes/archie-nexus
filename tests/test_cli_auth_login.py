import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
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


def test_github_login_reads_both_tokens_from_json_stdin(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ignored")
    monkeypatch.setattr(
        "sys.stdin",
        MagicMock(
            isatty=lambda: False,
            read=lambda: json.dumps({"personal_token": "p-secret", "work_token": "w-secret"}),
        ),
    )
    response = _response()
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://127.0.0.1:7600"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("github")
    put.assert_called_once_with(
        "http://127.0.0.1:7600/auth/credential/github",
        json={"personal_token": "p-secret", "work_token": "w-secret"},
        timeout=10,
    )


def test_github_login_prompts_for_both_tokens_hidden(monkeypatch):
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    prompts = []

    def prompt(text, **kwargs):
        prompts.append((text, kwargs.get("hide_input")))
        return {"github personal_token": "p", "github work_token": "w"}[text]

    response = _response()
    with (
        patch("archie_cli.auth.click.prompt", side_effect=prompt),
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("github")
    assert prompts == [("github personal_token", True), ("github work_token", True)]
    assert put.call_args.kwargs["json"] == {"personal_token": "p", "work_token": "w"}


def test_scalr_login_uses_environment(monkeypatch):
    monkeypatch.setenv("SCALR_HOSTNAME", "scalr.example")  # a setting now (D9); not read
    monkeypatch.setenv("SCALR_TOKEN", "scalr-secret")
    monkeypatch.setenv("SCALR_ACCOUNT", "account-1")
    response = _response()
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=response) as put,
    ):
        _set_static_from_login("scalr")
    assert put.call_args.kwargs["json"] == {"token": "scalr-secret"}


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


def test_login_datadog_takes_static_path():
    with (
        patch("archie_cli.auth._set_static_from_login") as static,
        patch("archie_cli.auth.httpx.post") as post,
    ):
        result = CliRunner().invoke(login, ["datadog"])
    assert result.exit_code == 0, result.output
    static.assert_called_once_with("datadog")
    post.assert_not_called()


def test_login_slack_takes_oauth_path():
    request = httpx.Request("POST", "http://test/auth/login/slack")
    with (
        patch("archie_cli.auth._set_static_from_login") as static,
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.post", side_effect=httpx.ConnectError("x", request=request)),
    ):
        result = CliRunner().invoke(login, ["slack"])
    assert result.exit_code != 0
    assert "Could not start OAuth login for slack" in result.output
    static.assert_not_called()


def _chain_session():
    frozen = MagicMock(access_key="AKIA", secret_key="secret", token="tok")
    session = MagicMock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = frozen
    session.client.return_value.get_caller_identity.return_value = {"Account": "123"}
    return session


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        (
            "aws",
            {"access_key_id": "AKIA", "secret_access_key": "secret", "session_token": "tok"},
        ),
        (
            "bedrock",
            {
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "secret",
                "aws_session_token": "tok",
            },
        ),
    ],
)
def test_aws_chain_providers_store_chain_credentials_in_field_order(service, expected):
    boto3 = pytest.importorskip("boto3")
    with (
        patch.object(boto3, "Session", return_value=_chain_session()),
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=_response()) as put,
    ):
        _set_static_from_login(service)
    put.assert_called_once_with(f"http://test/auth/credential/{service}", json=expected, timeout=10)


def test_aws_chain_login_is_selected_by_provider_login_not_name(monkeypatch):
    """A provider with login=aws_chain uses the chain regardless of its name."""
    from archie_shared.credentials.providers import PROVIDERS, StaticProvider

    boto3 = pytest.importorskip("boto3")
    custom = StaticProvider(name="custom", fields=["a", "b", "c"], login="aws_chain")
    monkeypatch.setitem(PROVIDERS, "custom", custom)
    with (
        patch.object(boto3, "Session", return_value=_chain_session()),
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=_response()) as put,
    ):
        result = CliRunner().invoke(login, ["custom"])
    assert result.exit_code == 0, result.output
    assert put.call_args.kwargs["json"] == {"a": "AKIA", "b": "secret", "c": "tok"}
