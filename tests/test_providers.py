"""Tests for the provider registry (providers.py)."""

from archie_shared.credentials.providers import PROVIDERS, OAuthProvider, StaticProvider


def test_all_services_present():
    """Registry covers all configured services."""
    expected = {
        "bedrock",
        "notion",
        "linear",
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
        "linear",
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
    assert incident_io.site is None


def test_datadog_uses_eu_site_and_key_fields():
    datadog = PROVIDERS["datadog"]
    assert isinstance(datadog, StaticProvider)
    assert datadog.fields == ["api_key", "app_key"]
    assert datadog.site == "app.datadoghq.eu"


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
