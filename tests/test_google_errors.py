import httpx
import pytest
from archie_agent.exec.tools import meet
from archie_agent.exec.tools.google_common import GoogleTransportError, _google_get


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
    monkeypatch.setattr(meet, "credential", lambda: type("C", (), {"access_token": "t"})())

    async def fake_request(operation, payload, token):
        return [{"state": "STARTED"}]

    monkeypatch.setattr(meet, "meet_request", fake_request)
    result = await meet.notes("meet:conference-1")
    assert result["state"] == "not_ready"
    assert result["message"] == "Meeting notes are not ready yet."
