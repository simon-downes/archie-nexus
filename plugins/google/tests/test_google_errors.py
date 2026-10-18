import dataclasses

import httpx
import pytest
from archie_agent.exec.plugin import CredentialSource, PluginContextError, install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_plugins.google import calendar, common, discovery, drive, mail, meet
from archie_plugins.google.common import (
    GoogleAuthorizationError,
    GoogleConfigurationError,
    GoogleTransportError,
    _google_get,
)
from archie_shared.credentials.models import CREDENTIAL_TYPES, OAuthCredential
from google_fakes import google_context


def _broken():
    raise RuntimeError("store unreadable")


@pytest.mark.parametrize(
    "credential",
    [
        OAuthCredential(),
        OAuthCredential(access_token=""),
        CREDENTIAL_TYPES["datadog"](api_key="a", app_key="b"),
    ],
)
async def test_credential_incomplete(google_context_installed, credential):
    install_context(google_context(google_context_installed, credential=credential))
    with pytest.raises(GoogleConfigurationError, match="^Google credentials are incomplete$"):
        common.credential()


async def test_credential_missing_is_incomplete(google_context_installed):
    install_context(make_plugin_context("google", tmp_path=google_context_installed))
    with pytest.raises(GoogleConfigurationError, match="^Google credentials are incomplete$"):
        common.credential()


async def test_credential_store_failure_is_unavailable(google_context_installed):
    ctx = google_context(google_context_installed)
    broken = CredentialSource("google", loader=_broken)
    install_context(dataclasses.replace(ctx, credentials=broken))
    with pytest.raises(
        GoogleConfigurationError, match="^Google credentials are unavailable or invalid$"
    ):
        common.credential()


async def test_credential_comes_from_the_context():
    assert common.credential().access_token == "t"


async def test_no_context_fails_closed():
    install_context(None)
    with pytest.raises(PluginContextError):
        common.credential()
    with pytest.raises(PluginContextError):
        common.require_read()


async def test_write_disabled_by_default_message():
    with pytest.raises(GoogleAuthorizationError, match="^Google writes are disabled by policy$"):
        common.require_write()


async def test_every_namespace_denied_when_reads_disabled(policy, monkeypatch):
    policy({"google": {"read": {"enabled": False}}})

    def unexpected(*args, **kwargs):
        raise AssertionError("provider called")

    for module, name in (
        (mail, "gmail_request"),
        (calendar, "calendar_request"),
        (drive, "drive_request"),
        (meet, "meet_request"),
        (discovery, "search_sources"),
    ):
        monkeypatch.setattr(module, name, unexpected)
    calls = (
        mail.search("x"),
        calendar.today(),
        drive.list(),
        meet.notes("meet:abc"),
        discovery.search("x"),
    )
    for call in calls:
        with pytest.raises(GoogleAuthorizationError, match="^Google reads are disabled by policy$"):
            await call


async def test_google_http_errors_include_status_and_safe_provider_detail(monkeypatch):
    response = httpx.Response(
        500,
        json={
            "error": {
                "code": 500,
                "message": "Backend unavailable",
                "errors": [{"reason": "backendError"}],
            }
        },
        request=httpx.Request("GET", "https://www.googleapis.com/drive/v3/files"),
    )

    async def request(*args, **kwargs):
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    with pytest.raises(GoogleTransportError, match="HTTP 500.*backendError.*Backend unavailable"):
        await _google_get("https://www.googleapis.com/drive/v3/files", "token")


async def test_google_transport_errors_include_safe_exception_context(monkeypatch):
    async def request(*args, **kwargs):
        raise httpx.ConnectError("DNS lookup failed")

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    with pytest.raises(GoogleTransportError, match="ConnectError.*DNS lookup failed"):
        await _google_get("https://www.googleapis.com/drive/v3/files", "token")


async def test_meet_not_ready_includes_user_visible_message(monkeypatch):

    async def fake_request(operation, payload, token):
        return [{"state": "STARTED"}]

    monkeypatch.setattr(meet, "meet_request", fake_request)
    result = await meet.notes("meet:conference-1")
    assert result["state"] == "not_ready"
    assert result["message"] == "Meeting notes are not ready yet."
