"""Regression tests for Jira review fixes; no live credentials or Jira access."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from archie_agent.exec.tools.jira.client import JiraClient, JiraValidationError
from archie_agent.exec.tools.jira.issues import JiraInputError, create_issue, update_issue
from archie_shared.credentials.models import JiraCredential


@pytest.mark.asyncio
async def test_client_accepts_empty_success_response():
    response = httpx.Response(204, request=httpx.Request("PUT", "https://jira.invalid"))
    client = JiraClient(JiraCredential(email="a@example.com", token="secret", cloud_id="cloud"))
    session = AsyncMock()
    session.__aenter__.return_value.request.return_value = response
    with patch.object(client, "session", return_value=session):
        assert await client.request("PUT", "/issue/PLAT-1") is None


@pytest.mark.asyncio
async def test_client_maps_validation_response():
    response = httpx.Response(422, request=httpx.Request("POST", "https://jira.invalid"))
    client = JiraClient(JiraCredential(email="a@example.com", token="secret", cloud_id="cloud"))
    session = AsyncMock()
    session.__aenter__.return_value.request.return_value = response
    with patch.object(client, "session", return_value=session):
        with pytest.raises(JiraValidationError):
            await client.request("POST", "/issue")


@pytest.mark.asyncio
async def test_ambiguous_assignee_prevents_creation(monkeypatch):
    monkeypatch.setenv(
        "ARCHIE_TOOL_POLICY",
        '{"jira":{"write":{"enabled":true,"scope":["PLAT"]}}}',
    )
    client = AsyncMock()
    client.request.return_value = [
        {"accountId": "1", "displayName": "Alex", "accountType": "atlassian"},
        {"accountId": "2", "displayName": "Alex", "accountType": "atlassian"},
    ]
    with patch("archie_agent.exec.tools.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        with pytest.raises(JiraInputError, match="ambiguous"):
            await create_issue("PLAT", "test", assignee="Alex")
    assert all(
        call.args[0] != "POST" or call.args[1] != "/issue" for call in client.request.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"summary": "New summary"}, {"summary": "New summary"}),
        ({"description": ""}, {"description": None}),
        ({"assignee": ""}, {"assignee": None}),
    ],
)
async def test_update_issue_tri_state_payload(monkeypatch, kwargs, expected):
    monkeypatch.setenv(
        "ARCHIE_TOOL_POLICY",
        '{"jira":{"write":{"enabled":true,"scope":["PLAT"]}}}',
    )
    client = AsyncMock()
    client.request.side_effect = [None, {"id": "1", "key": "PLAT-1", "fields": {}}]
    with patch("archie_agent.exec.tools.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await update_issue("PLAT-1", **kwargs)
    payload = client.request.call_args_list[0].kwargs["json"]["fields"]
    assert payload == expected


@pytest.mark.asyncio
async def test_update_issue_none_is_unchanged_and_requires_a_change():
    with pytest.raises(JiraInputError, match="at least one"):
        await update_issue("PLAT-1")


@pytest.mark.asyncio
async def test_update_issue_resolves_username_to_account_id(monkeypatch):
    monkeypatch.setenv(
        "ARCHIE_TOOL_POLICY",
        '{"jira":{"write":{"enabled":true,"scope":["PLAT"]}}}',
    )
    client = AsyncMock()
    client.request.side_effect = [
        [{"accountId": "acct-1", "username": "asmith", "accountType": "atlassian"}],
        None,
        {"id": "1", "key": "PLAT-1", "fields": {}},
    ]
    with patch("archie_agent.exec.tools.jira.issues.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        await update_issue("PLAT-1", assignee="asmith")
    assert client.request.call_args_list[1].kwargs["json"]["fields"] == {
        "assignee": {"accountId": "acct-1"}
    }
