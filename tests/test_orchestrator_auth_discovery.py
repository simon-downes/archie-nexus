import pytest
from archie_orchestrator.auth import AuthService
from archie_shared.schemas import NexusConfig


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class Transport:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == "GET" and url.endswith("oauth-protected-resource"):
            return Response({"authorization_servers": ["https://auth.example"]})
        if method == "GET":
            return Response({
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "registration_endpoint": "https://auth.example/register",
            })
        if method == "POST" and url.endswith("register"):
            return Response({"client_id": "dynamic-client"})
        raise AssertionError((method, url))


@pytest.mark.asyncio
async def test_discovery_and_registration_are_used_for_server_url_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    transport = Transport()
    service = AuthService(NexusConfig(), transport=transport)
    flow, url = await service.start_login("notion", "http://test/auth/callback/notion")
    assert flow.client_id == "dynamic-client"
    assert "dynamic-client" in url
    assert any(call[1].endswith("register") for call in transport.calls)
    assert "dynamic-client" in (tmp_path / "credentials.yaml").read_text()
