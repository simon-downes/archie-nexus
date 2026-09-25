import pytest
from archie_orchestrator.auth import AuthError, AuthService
from archie_shared.credentials.api import AuthConfig, AuthProviderOverride
from archie_shared.schemas import NexusConfig


class FakeResponse:
    def __init__(self, data, *, status_code=200):
        self.data = data
        self.status_code = status_code
        self.is_error = status_code >= 400

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeTransport:
    async def request(self, method, url, **kwargs):
        assert method == "POST"
        assert kwargs["data"]["code_verifier"]
        return FakeResponse(
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )


class FailingTokenTransport:
    async def request(self, method, url, **kwargs):
        return FakeResponse(
            {"error": "invalid_client", "error_description": "The OAuth client is not valid"},
            status_code=400,
        )


@pytest.mark.asyncio
async def test_google_login_requires_client_id(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    service = AuthService(NexusConfig())
    with pytest.raises(AuthError, match="google.*client_id"):
        await service.start_login("google", "http://test/auth/callback/google")


@pytest.mark.asyncio
async def test_token_exchange_preserves_sanitized_provider_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = NexusConfig(
        auth=AuthConfig(providers={"google": AuthProviderOverride(client_id="client")})
    )
    service = AuthService(config, transport=FailingTokenTransport())
    flow, _ = await service.start_login("google", "http://test/auth/callback/google")
    with pytest.raises(AuthError, match="invalid_client: The OAuth client is not valid"):
        await service.complete(flow, "code")


@pytest.mark.asyncio
async def test_login_callback_contract_persists_normalized_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = NexusConfig(
        auth=AuthConfig(providers={"google": AuthProviderOverride(client_id="client")})
    )
    service = AuthService(config, transport=FakeTransport())
    flow, url = await service.start_login("google", "http://test/auth/callback/google")
    assert "code_challenge_method=S256" in url
    assert "client_id=client" in url
    status = await service.complete(flow, "code")
    assert status.state == "valid"
    stored = (tmp_path / "credentials.yaml").read_text()
    assert "access" in stored and "refresh" in stored
