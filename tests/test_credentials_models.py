import msgspec
import pytest
from archie_shared.credentials.api import (
    CredentialStatus,
    OAuthProviderResponse,
    StaticProviderResponse,
)
from archie_shared.credentials.models import (
    CREDENTIAL_TYPES,
    BedrockCredential,
    OAuthCredential,
)


def test_all_agent_kit_providers_have_typed_definitions():
    from archie_shared.credentials.providers import PROVIDERS

    assert set(PROVIDERS) == {
        "cloudflare",
        "notion",
        "slack",
        "github",
        "aws",
        "scalr",
        "jira",
        "google",
        "bedrock",
        "datadog",
        "incident_io",
        "openai",
        "kiro",
    }
    assert set(CREDENTIAL_TYPES) == set(PROVIDERS)
    assert CREDENTIAL_TYPES["bedrock"] is BedrockCredential
    for name in ("notion", "slack", "google"):
        assert CREDENTIAL_TYPES[name] is OAuthCredential
    assert CREDENTIAL_TYPES["datadog"].__struct_fields__ == ("api_key", "app_key")
    assert CREDENTIAL_TYPES["incident_io"].__struct_fields__ == ("api_key",)
    assert CREDENTIAL_TYPES["aws"].__struct_fields__ == (
        "access_key_id",
        "secret_access_key",
        "session_token",
    )
    assert CREDENTIAL_TYPES["jira"].__struct_fields__ == ("email", "token", "cloud_id")
    assert CREDENTIAL_TYPES["github"].__struct_fields__ == ("personal_token", "work_token")
    assert CREDENTIAL_TYPES["scalr"].__struct_fields__ == ("token",)
    assert CREDENTIAL_TYPES["cloudflare"].__struct_fields__ == ("api_token",)
    assert CREDENTIAL_TYPES["aws"] is not CREDENTIAL_TYPES["bedrock"]
    assert CREDENTIAL_TYPES["incident_io"].__name__ == "IncidentIoCredential"


def test_generated_credentials_have_static_provider_fields():
    from archie_shared.credentials.providers import PROVIDERS, StaticProvider

    for name, provider in PROVIDERS.items():
        if isinstance(provider, StaticProvider):
            assert list(CREDENTIAL_TYPES[name].__struct_fields__) == provider.fields, name


def test_provider_and_status_responses_are_secret_free_and_strict():
    provider = StaticProviderResponse(name="github", fields=["personal_token", "work_token"])
    status = CredentialStatus(
        provider="github", auth_type="static", configured=True, state="configured"
    )
    encoded = msgspec.json.encode([provider, status]).decode()
    assert "token" in encoded
    assert "secret" not in encoded.lower()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"name":"github","fields":[],"unexpected":"x"}', type=StaticProviderResponse
        )


def test_oauth_response_contains_required_metadata_without_secret():
    response = OAuthProviderResponse(
        name="slack",
        server_url=None,
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        scopes=[],
        token_path="authed_user.access_token",
        refresh_token_path="authed_user.refresh_token",
        extra_params={"user_scope": "users:read"},
    )
    encoded = msgspec.json.encode(response).decode()
    assert "client_secret" not in encoded
    assert "user_scope" in encoded
