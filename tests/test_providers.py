"""Tests for the provider registry (providers.py)."""

from archie_shared.credentials.providers import PROVIDERS, OAuthProvider, StaticProvider


def test_all_services_present():
    """Registry covers all configured services."""
    expected = {
        "bedrock",
        "notion",
        "slack",
        "github",
        "aws",
        "scalr",
        "jira",
        "datadog",
        "incident_io",
        "google",
    }
    assert set(PROVIDERS.keys()) == expected


def test_static_providers():
    """Static providers have correct type and fields."""
    static_services = {
        "bedrock",
        "github",
        "aws",
        "scalr",
        "jira",
        "datadog",
        "incident_io",
    }
    for name in static_services:
        provider = PROVIDERS[name]
        assert isinstance(provider, StaticProvider), f"{name} should be StaticProvider"
        assert provider.name == name
        assert len(provider.fields) > 0, f"{name} should have at least one field"


def test_oauth_providers():
    """OAuth providers have correct type and endpoints."""
    oauth_services = {"notion", "slack", "google"}
    for name in oauth_services:
        provider = PROVIDERS[name]
        assert isinstance(provider, OAuthProvider), f"{name} should be OAuthProvider"
        assert provider.name == name
        assert provider.can_refresh_noninteractive is True


def test_incident_io_uses_api_key_only():
    incident_io = PROVIDERS["incident_io"]
    assert isinstance(incident_io, StaticProvider)
    assert incident_io.fields == ["api_key"]


def test_datadog_uses_key_fields_and_site_is_not_a_provider_field():
    datadog = PROVIDERS["datadog"]
    assert isinstance(datadog, StaticProvider)
    assert datadog.fields == ["api_key", "app_key"]
    assert "site" not in StaticProvider.__struct_fields__


def test_bedrock_not_refreshable():
    """Bedrock cannot be refreshed non-interactively."""
    assert PROVIDERS["bedrock"].can_refresh_noninteractive is False


def test_slack_has_custom_token_paths():
    """Slack uses nested token paths for its non-standard response."""
    slack = PROVIDERS["slack"]
    assert isinstance(slack, OAuthProvider)
    assert slack.token_path == "authed_user.access_token"
    assert slack.refresh_token_path == "authed_user.refresh_token"


def test_slack_requests_scopes_for_every_tool():
    """Slack user scopes cover each slack.* tool's API calls, including email."""
    slack = PROVIDERS["slack"]
    assert isinstance(slack, OAuthProvider)
    scopes = slack.extra_params["user_scope"].split(" ")
    assert len(scopes) == len(set(scopes))
    assert set(scopes) == {
        "channels:read",
        "groups:read",
        "im:read",
        "mpim:read",
        "channels:history",
        "groups:history",
        "im:history",
        "mpim:history",
        "users:read",
        "users:read.email",
        "search:read.public",
        "search:read.private",
        "search:read.im",
        "search:read.mpim",
        "search:read.files",
        "search:read.users",
        "reactions:read",
        "reactions:write",
        "chat:write",
    }
    # The legacy bundled scope is not used by assistant.search.context.
    assert "search:read" not in scopes


def test_slack_search_scopes_are_requested():
    """Every scope the search tool can require is in the OAuth request."""
    from archie_agent.exec.tools.slack.search import required_scopes

    requested = set(PROVIDERS["slack"].extra_params["user_scope"].split(" "))
    needed = required_scopes(
        ["messages", "files", "users"], ["public_channel", "private_channel", "im", "mpim"]
    )
    assert set(needed) <= requested


def test_google_has_required_scopes():
    """Google has the complete Workspace tooling OAuth scope set configured."""
    google = PROVIDERS["google"]
    assert isinstance(google, OAuthProvider)
    assert google.scopes is not None
    assert set(google.scopes) == {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/drive.meet.readonly",
    }


def test_notion_uses_discovery():
    """Notion uses server_url for endpoint discovery."""
    notion = PROVIDERS["notion"]
    assert isinstance(notion, OAuthProvider)
    assert notion.server_url is not None
    assert notion.authorization_endpoint is None  # discovered at login time
    assert notion.server_url == "https://mcp.notion.com"


_SLACK_USER_SCOPE = (
    "channels:read groups:read im:read mpim:read "
    "channels:history groups:history im:history mpim:history "
    "users:read users:read.email "
    "search:read.public search:read.private search:read.im search:read.mpim "
    "search:read.files search:read.users "
    "reactions:read reactions:write "
    "chat:write"
)

# Hard-coded snapshot of the hand-written registry before plan 070 M3; the
# manifest-generated registry must reproduce it exactly.
EXPECTED_PROVIDERS = {
    "bedrock": StaticProvider(
        name="bedrock",
        fields=["aws_access_key_id", "aws_secret_access_key", "aws_session_token"],
        env={
            "aws_access_key_id": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "aws_session_token": "AWS_SESSION_TOKEN",
        },
        set_env=False,
        login="aws_chain",
    ),
    "aws": StaticProvider(
        name="aws",
        fields=["access_key_id", "secret_access_key", "session_token"],
        env={
            "access_key_id": "AWS_ACCESS_KEY_ID",
            "secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "session_token": "AWS_SESSION_TOKEN",
        },
        set_env=True,
        login="aws_chain",
    ),
    "datadog": StaticProvider(name="datadog", fields=["api_key", "app_key"]),
    "github": StaticProvider(name="github", fields=["personal_token", "work_token"]),
    "google": OAuthProvider(
        name="google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/meetings.space.readonly",
            "https://www.googleapis.com/auth/drive.meet.readonly",
        ],
        extra_params={"access_type": "offline", "prompt": "consent"},
    ),
    "incident_io": StaticProvider(name="incident_io", fields=["api_key"]),
    "jira": StaticProvider(name="jira", fields=["email", "token", "cloud_id"]),
    "notion": OAuthProvider(name="notion", server_url="https://mcp.notion.com"),
    # D9: hostname/account moved to plugins.scalr.settings (env-projected).
    "scalr": StaticProvider(
        name="scalr", fields=["token"], env={"token": "SCALR_TOKEN"}, set_env=True
    ),
    "slack": OAuthProvider(
        name="slack",
        authorization_endpoint="https://slack.com/oauth/v2/authorize",
        token_endpoint="https://slack.com/api/oauth.v2.access",
        token_path="authed_user.access_token",
        refresh_token_path="authed_user.refresh_token",
        expires_in_path="authed_user.expires_in",
        extra_params={"user_scope": _SLACK_USER_SCOPE},
    ),
}


def test_generated_providers_match_previous_registry_exactly():
    """Manifest-generated PROVIDERS reproduce every pre-M3 value, field by field."""
    assert list(PROVIDERS) == list(EXPECTED_PROVIDERS)  # bedrock first, then plugin ids sorted
    for name, expected in EXPECTED_PROVIDERS.items():
        actual = PROVIDERS[name]
        assert type(actual) is type(expected), name
        for field in expected.__struct_fields__:
            assert getattr(actual, field) == getattr(expected, field), f"{name}.{field}"


def test_only_bedrock_is_hand_written():
    """Every non-bedrock provider has a plugin manifest declaring its credential."""
    from archie_shared.plugins import load_manifests

    with_credential = {
        pid for pid, loaded in load_manifests().items() if loaded.manifest.credential
    }
    assert set(PROVIDERS) - with_credential == {"bedrock"}


def test_aws_chain_login_providers():
    assert {n for n, p in PROVIDERS.items() if getattr(p, "login", None) == "aws_chain"} == {
        "aws",
        "bedrock",
    }
