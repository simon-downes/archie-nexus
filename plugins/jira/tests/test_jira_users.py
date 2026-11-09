"""User search, assignee resolution, validation error detail and status dedupe; no network."""

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from archie_plugins.jira.client import JiraClient, JiraPolicyError, JiraValidationError
from archie_plugins.jira.issues import JiraInputError, create_issue, search_users, update_issue
from archie_plugins.jira.tools import get_project
from archie_shared.credentials.models import CREDENTIAL_TYPES

WRITE = {"jira": {"write": {"enabled": True, "scope": ["PLAT"]}}}
ACCOUNT = "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077"
ISSUE = {"id": "1", "key": "PLAT-1", "fields": {}}


def _split(call):
    method, path = call.args[0], call.args[1]
    parts = urlsplit(path)
    return method, parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def _client(*responses):
    client = AsyncMock()
    client.request.side_effect = list(responses)
    return client


async def _run(client, coro_factory):
    with (
        patch("archie_plugins.jira.issues.jira_client") as issues_factory,
        patch("archie_plugins.jira.tools.jira_client") as tools_factory,
    ):
        issues_factory.return_value.__aenter__.return_value = client
        tools_factory.return_value.__aenter__.return_value = client
        return await coro_factory()


def _user(account="acct-1", name="Simon Downes", email="", **extra):
    return {
        "accountId": account,
        "displayName": name,
        "emailAddress": email,
        "accountType": "atlassian",
        "active": True,
        **extra,
    }


# --- search_users -----------------------------------------------------------------------------


async def test_search_users_without_project_uses_user_search():
    client = _client([_user(), _user("app", "Bot", accountType="app")])
    result = await _run(client, lambda: search_users("Simon"))
    method, path, params = _split(client.request.call_args_list[0])
    assert (method, path) == ("GET", "/user/search")
    assert params["query"] == "Simon" and "project" not in params
    assert result == [{"account_id": "acct-1", "display_name": "Simon Downes", "email": ""}]
    assert client.request.call_count == 1


async def test_search_users_with_project_uses_assignable_search_and_pages_past_short_pages():
    client = _client([_user("a")], [_user("b")], [])
    result = await _run(client, lambda: search_users("simon", limit=5, project="plat"))
    calls = [_split(call) for call in client.request.call_args_list]
    assert [c[1] for c in calls] == ["/user/assignable/search"] * 3
    assert all(c[2]["project"] == "PLAT" for c in calls)
    assert [c[2]["startAt"] for c in calls] == ["0", "50", "100"]
    assert [u["account_id"] for u in result] == ["a", "b"]


async def test_search_users_project_respects_read_scope(policy):
    policy({"jira": {"read": {"enabled": True, "scope": ["OPS"]}}})
    client = _client()
    with pytest.raises(JiraPolicyError):
        await _run(client, lambda: search_users("simon", project="PLAT"))
    client.request.assert_not_called()


async def test_search_users_drops_inactive():
    client = _client([_user("a"), _user("b", active=False)])
    result = await _run(client, lambda: search_users("simon"))
    assert [u["account_id"] for u in result] == ["a"]


# --- assignee resolution ----------------------------------------------------------------------


async def test_create_issue_resolves_assignee_within_project(policy):
    policy(WRITE)
    client = _client([_user(name="Simon Downes")], {"key": "PLAT-9", "id": "9"})
    await _run(client, lambda: create_issue("PLAT", "t", assignee="Simon Downes"))
    method, path, params = _split(client.request.call_args_list[0])
    assert path == "/user/assignable/search"
    assert params == {"project": "PLAT", "query": "Simon Downes", "maxResults": "50"}
    payload = client.request.call_args_list[1].kwargs["json"]["fields"]
    assert payload["assignee"] == {"accountId": "acct-1"}


async def test_update_issue_resolves_assignee_on_the_issue(policy):
    policy(WRITE)
    client = _client([_user(name="Simon Downes")], None, ISSUE)
    await _run(client, lambda: update_issue("PLAT-1", assignee="Simon Downes"))
    _, path, params = _split(client.request.call_args_list[0])
    assert path == "/user/assignable/search"
    assert params["issueKey"] == "PLAT-1" and "project" not in params


async def test_account_id_is_looked_up_by_account_id(policy):
    policy(WRITE)
    client = _client([_user(ACCOUNT)], None, ISSUE)
    await _run(client, lambda: update_issue("PLAT-1", assignee=ACCOUNT))
    _, _, params = _split(client.request.call_args_list[0])
    assert params["accountId"] == ACCOUNT and "query" not in params
    assert client.request.call_args_list[1].kwargs["json"]["fields"]["assignee"] == {
        "accountId": ACCOUNT
    }


async def test_email_resolves_when_jira_hides_it_and_one_user_matches(policy):
    policy(WRITE)
    client = _client([_user(email="")], None, ISSUE)
    await _run(client, lambda: update_issue("PLAT-1", assignee="simon.downes@example.com"))
    assert client.request.call_args_list[1].kwargs["json"]["fields"]["assignee"] == {
        "accountId": "acct-1"
    }


async def test_email_with_hidden_addresses_and_several_matches_is_rejected(policy):
    policy(WRITE)
    client = _client([_user("a"), _user("b")])
    with pytest.raises(JiraInputError, match="no assignable user matches"):
        await _run(client, lambda: update_issue("PLAT-1", assignee="simon@example.com"))
    assert client.request.call_count == 1


async def test_email_does_not_fall_back_to_a_user_with_a_different_visible_email(policy):
    policy(WRITE)
    client = _client([_user(email="someone.else@example.com")])
    with pytest.raises(JiraInputError, match="unavailable"):
        await _run(client, lambda: update_issue("PLAT-1", assignee="simon@example.com"))


async def test_display_name_needs_an_exact_match(policy):
    policy(WRITE)
    client = _client([_user(name="Simon Downes")])
    with pytest.raises(JiraInputError, match="no assignable user matches"):
        await _run(client, lambda: update_issue("PLAT-1", assignee="Simon"))


async def test_inactive_assignee_is_rejected(policy):
    policy(WRITE)
    client = _client([_user(name="Simon Downes", active=False)])
    with pytest.raises(JiraInputError, match="unavailable"):
        await _run(client, lambda: update_issue("PLAT-1", assignee="Simon Downes"))


# --- validation error detail ------------------------------------------------------------------


def _jira_client_returning(response):
    credential = CREDENTIAL_TYPES["jira"](email="a@example.com", token="secret", cloud_id="cloud")
    client = JiraClient(credential)
    session = AsyncMock()
    session.__aenter__.return_value.request.return_value = response
    return client, session


async def test_validation_error_includes_jira_messages():
    body = {
        "errorMessages": ["The issue key or project must be provided."],
        "errors": {"assignee": "User 'x' cannot be assigned issues."},
    }
    response = httpx.Response(400, json=body, request=httpx.Request("GET", "https://j.invalid"))
    client, session = _jira_client_returning(response)
    with patch.object(client, "session", return_value=session):
        with pytest.raises(JiraValidationError) as error:
            await client.request("GET", "/user/assignable/search")
    message = str(error.value)
    assert message.startswith("Jira rejected the request fields: ")
    assert "project must be provided" in message
    assert "assignee: User 'x' cannot be assigned issues." in message


async def test_validation_error_detail_is_bounded_and_tolerates_non_json():
    long = {"errorMessages": ["x" * 1000, "y" * 1000, "z" * 1000]}
    response = httpx.Response(400, json=long, request=httpx.Request("GET", "https://j.invalid"))
    client, session = _jira_client_returning(response)
    with patch.object(client, "session", return_value=session):
        with pytest.raises(JiraValidationError) as error:
            await client.request("GET", "/x")
    detail = str(error.value).removeprefix("Jira rejected the request fields: ")
    assert len(detail) <= 500 and "x" * 201 not in detail

    plain = httpx.Response(400, text="<html>", request=httpx.Request("GET", "https://j.invalid"))
    client, session = _jira_client_returning(plain)
    with patch.object(client, "session", return_value=session):
        with pytest.raises(JiraValidationError, match=r"^Jira rejected the request fields$"):
            await client.request("GET", "/x")


# --- get_project statuses ---------------------------------------------------------------------


async def test_get_project_lists_each_status_once():
    statuses = [
        {"name": "Task", "statuses": [{"id": "1", "name": "Backlog"}, {"id": "3", "name": "Done"}]},
        {"name": "Bug", "statuses": [{"id": "1", "name": "Backlog"}, {"id": "2", "name": "Ready"}]},
    ]
    client = _client({"id": "10", "key": "PLAT", "name": "Platform"}, [], statuses)
    result = await _run(client, lambda: get_project("PLAT"))
    assert result["statuses"] == [
        {"id": "1", "name": "Backlog"},
        {"id": "3", "name": "Done"},
        {"id": "2", "name": "Ready"},
    ]
