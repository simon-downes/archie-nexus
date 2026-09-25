"""Tests for the provider registry (providers.py)."""

from archie_shared.credentials.providers import PROVIDERS, OAuthProvider, StaticProvider


def test_all_services_present():
    """Registry covers all 9 services (8 agent-kit + bedrock)."""
    expected = {"bedrock", "notion", "linear", "slack", "github", "aws", "scalr", "jira", "google"}
    assert set(PROVIDERS.keys()) == expected


def test_static_providers():
    """Static providers have correct type and fields."""
    static_services = {"bedrock", "linear", "github", "aws", "scalr", "jira"}
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


def test_bedrock_not_refreshable():
    """Bedrock cannot be refreshed non-interactively."""
    assert PROVIDERS["bedrock"].can_refresh_noninteractive is False


def test_slack_has_custom_token_paths():
    """Slack uses nested token paths for its non-standard response."""
    slack = PROVIDERS["slack"]
    assert isinstance(slack, OAuthProvider)
    assert slack.token_path == "authed_user.access_token"
    assert slack.refresh_token_path == "authed_user.refresh_token"


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
