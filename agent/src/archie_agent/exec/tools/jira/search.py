"""Structured, scope-safe Jira issue search."""

from __future__ import annotations

from archie_agent.exec.tools import ToolError, tool

from .client import JiraResponseError, jira_client
from .formatting import issue_fields
from .policy import policies, project_key, require_read


class JiraSearchValidationError(ToolError):
    pass


def _escape(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _jql(
    project: str | None,
    status: str | None,
    assignee: str | None,
    label: str | None,
    created_before: str | None,
    created_after: str | None,
    updated_before: str | None,
    updated_after: str | None,
    parent: str | None,
    scope: set[str] | None,
) -> str:
    clauses = []
    if project:
        if scope is not None and project_key(project) not in scope:
            raise JiraSearchValidationError("Jira project is outside the configured read scope")
        clauses.append(f"project = {_escape(project_key(project))}")
    elif scope:
        clauses.append("project in (" + ", ".join(_escape(v) for v in sorted(scope)) + ")")
    for field, value in (
        ("status =", status),
        ("assignee =", assignee),
        ("labels =", label),
        ("created <=", created_before),
        ("created >=", created_after),
        ("updated <=", updated_before),
        ("updated >=", updated_after),
        ("parent", parent),
    ):
        if value:
            clauses.append(f"{field} {_escape(value)}")
    return " AND ".join(clauses) or "ORDER BY updated DESC"


@tool(
    guidelines=("Use structured Jira filters; raw JQL is not available.",),
    native=False,
    exec_docs=False,
    namespace="jira",
)
async def list_issues(
    project: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    label: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
    updated_before: str | None = None,
    updated_after: str | None = None,
    parent: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search issues using structured filters."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise JiraSearchValidationError("limit must be 1-100")
    values = {
        "project": project,
        "status": status,
        "assignee": assignee,
        "label": label,
        "created_before": created_before,
        "created_after": created_after,
        "updated_before": updated_before,
        "updated_after": updated_after,
        "parent": parent,
    }
    for name, value in values.items():
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 200
        ):
            raise JiraSearchValidationError(f"{name} must be 1-200 characters")
    require_read(project)
    scope = policies()[0]["scope"]
    jql = _jql(
        project,
        status,
        assignee,
        label,
        created_before,
        created_after,
        updated_before,
        updated_after,
        parent,
        scope,
    )
    results, token = [], None
    async with jira_client() as client:
        while len(results) < limit:
            body = {
                "jql": jql,
                "maxResults": min(50, limit - len(results)),
                "fields": [
                    "summary",
                    "status",
                    "assignee",
                    "parent",
                    "priority",
                    "issuetype",
                    "labels",
                    "created",
                    "updated",
                ],
            }
            if token:
                body["nextPageToken"] = token
            data = await client.request("POST", "/search/jql", json=body)
            if not isinstance(data, dict) or not isinstance(data.get("issues", []), list):
                raise JiraResponseError("Jira returned malformed search results")
            results.extend(issue_fields(item) for item in data["issues"] if isinstance(item, dict))
            token = data.get("nextPageToken")
            if not token:
                break
    return results[:limit]
