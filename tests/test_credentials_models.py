import msgspec
import pytest
from archie_shared.credentials.api import (
    CredentialStatus,
    OAuthProviderResponse,
    StaticProviderResponse,
)
from archie_shared.credentials.models import CREDENTIAL_TYPES, AwsCredential, BedrockCredential


def test_all_agent_kit_providers_have_typed_definitions():
    from archie_shared.credentials.providers import PROVIDERS

    assert set(PROVIDERS) == {"notion", "linear", "slack", "github", "aws", "scalr", "jira", "google", "bedrock"}
    assert CREDENTIAL_TYPES["aws"] is AwsCredential
    assert CREDENTIAL_TYPES["bedrock"] is BedrockCredential
    assert "account" in CREDENTIAL_TYPES["scalr"].__struct_fields__
    assert CREDENTIAL_TYPES["aws"] is not CREDENTIAL_TYPES["bedrock"]


def test_provider_and_status_responses_are_secret_free_and_strict():
    provider = StaticProviderResponse(name="github", fields=["token"])
    status = CredentialStatus(
        provider="github", auth_type="static", configured=True, state="configured"
    )
    encoded = msgspec.json.encode([provider, status]).decode()
    assert "token" in encoded
    assert "secret" not in encoded.lower()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b'{"name":"github","fields":[],"unexpected":"x"}', type=StaticProviderResponse)


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
