"""Credential-free Jira foundation and safety tests."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.jira.client import JiraConfigurationError
from archie_plugins.jira.issues import get_issue
from archie_plugins.jira.policy import JiraValidationError, issue_key
from archie_plugins.jira.search import JiraSearchValidationError, list_issues
from archie_plugins.jira.tools import get_project, list_projects


async def test_missing_credentials_makes_no_request(policy):
    policy({}, credential=None)
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as request:
        with pytest.raises(JiraConfigurationError):
            await list_projects()
        request.assert_not_awaited()


async def test_scoped_project_listing_filters_results(policy):
    policy({"jira": {"read": {"enabled": True, "scope": ["PLAT"]}}})
    response = {
        "values": [
            {"id": "1", "key": "OTHER", "name": "Other"},
            {"id": "2", "key": "PLAT", "name": "Platform"},
        ],
        "isLast": True,
    }
    client = AsyncMock()
    client.request.return_value = response
    with patch("archie_plugins.jira.tools.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        result = await list_projects(limit=10)
    assert result == [{"id": "2", "key": "PLAT", "name": "Platform"}]


async def test_out_of_scope_project_is_rejected_before_client(policy):
    policy({"jira": {"read": {"enabled": True, "scope": ["PLAT"]}}})
    with patch("archie_plugins.jira.tools.jira_client") as factory:
        with pytest.raises(Exception, match="outside.*scope"):
            await get_project("OTHER")
        factory.assert_not_called()


def test_issue_key_requires_full_grammar():
    assert issue_key("plat-42") == "PLAT-42"
    for value in ("PLAT-0", "PLAT-x", "PLAT-1/2", "PLAT-1?x", "PLAT-X-2"):
        with pytest.raises(JiraValidationError):
            issue_key(value)


async def test_malformed_issue_key_makes_no_request(policy):
    policy({"jira": {"read": {"enabled": True}}})
    with patch("archie_plugins.jira.issues.jira_client") as factory:
        with pytest.raises(JiraValidationError):
            await get_issue("PLAT-1/2")
        factory.assert_not_called()


async def test_search_escapes_query_and_scope(policy):
    policy({"jira": {"read": {"enabled": True, "scope": ["PLAT"]}}})
    client = AsyncMock()
    client.request.return_value = {"issues": [], "nextPageToken": None}
    with patch("archie_plugins.jira.search.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await list_issues(status="Open", limit=1)
    body = client.request.call_args.kwargs["json"]
    assert 'project in ("PLAT")' in body["jql"]
    assert 'status = "Open"' in body["jql"]


async def test_invalid_limit_uses_jira_error():
    with pytest.raises(JiraSearchValidationError):
        await list_issues(limit=0)
