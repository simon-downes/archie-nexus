"""Public Jira project discovery functions."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from archie_agent.exec.tools import ToolError, tool

from .client import JiraResponseError, jira_client
from .policy import project_key, require_read

_MAX = 100


class JiraInputError(ToolError):
    pass


def _text(value: Any, limit: int = 8000) -> str:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return " ".join(_text(v, limit) for v in value.values())[:limit]
    if isinstance(value, list):
        return " ".join(_text(v, limit) for v in value)[:limit]
    return str(value)[:limit]


def _project(item: dict) -> dict:
    key, identifier = str(item.get("key", "")).upper(), str(item.get("id", ""))
    if not key or not identifier:
        raise JiraResponseError("Jira returned malformed project metadata")
    return {"key": key, "name": _text(item.get("name", "")), "id": identifier}


@tool(
    guidelines=("Use Jira only through the `jira` namespace and never provide credentials.",),
    native=False,
    exec_docs=False,
    namespace="jira",
)
async def list_projects(limit: int = 50) -> list[dict]:
    """List visible Jira projects within the configured read scope."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX:
        raise JiraInputError("limit must be 1-100")
    require_read()
    from .policy import policies

    scope = policies()[0]["scope"]
    result, start = [], 0
    async with jira_client() as client:
        while len(result) < limit:
            query = urlencode({"startAt": start, "maxResults": min(50, limit)})
            data = await client.request("GET", f"/project/search?{query}")
            if not isinstance(data, dict) or not isinstance(data.get("values"), list):
                raise JiraResponseError("Jira returned malformed projects")
            values = data["values"]
            for item in values:
                if isinstance(item, dict):
                    obj = _project(item)
                    if scope is None or obj["key"] in scope:
                        result.append(obj)
                    if len(result) >= limit:
                        break
            if data.get("isLast", True) or not values:
                break
            start += len(values)
    return result[:limit]


@tool(
    guidelines=("Use Jira only through the `jira` namespace and never provide credentials.",),
    native=False,
    exec_docs=False,
    namespace="jira",
)
async def get_project(project: str) -> dict:
    """Get Jira project metadata, issue types, and statuses."""
    key = project_key(project)
    require_read(key)
    async with jira_client() as client:
        metadata = await client.request("GET", f"/project/{key}")
        if not isinstance(metadata, dict) or not metadata.get("id"):
            raise JiraResponseError("Jira returned malformed project metadata")
        issue_types = await client.request(
            "GET", f"/issuetype/project?{urlencode({'projectId': str(metadata['id'])})}"
        )
        statuses = await client.request("GET", f"/project/{key}/statuses")
    values = (
        issue_types.get("values", issue_types) if isinstance(issue_types, dict) else issue_types
    )
    if not isinstance(values, list) or not isinstance(statuses, list):
        raise JiraResponseError("Jira returned malformed project metadata")
    normalized_types = [
        {"id": str(item.get("id")), "name": _text(item.get("name", ""))}
        for item in values
        if isinstance(item, dict) and item.get("id")
    ]
    normalized_statuses = []
    for group in statuses:
        if not isinstance(group, dict):
            continue
        for item in group.get("statuses", []):
            if isinstance(item, dict) and item.get("id"):
                normalized_statuses.append(
                    {"id": str(item["id"]), "name": _text(item.get("name", ""))}
                )
    result = {
        "project": _project(metadata),
        "issue_types": normalized_types,
        "statuses": normalized_statuses,
    }
    return result
