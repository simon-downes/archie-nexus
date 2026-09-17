"""Issue reads, comments, users, and guarded Jira mutations."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from archie_agent.exec.tools import ToolError, tool

from .client import JiraResponseError, JiraTransportError, jira_client
from .formatting import adf_text, attachment, comment, gmt, issue_fields
from .policy import issue_key as validate_issue_key
from .policy import project_key, require_read, require_write

_MAX_TEXT = 8000


class JiraMutationIndeterminateError(ToolError):
    pass


class JiraInputError(ToolError):
    pass


def _issue_project(key: str) -> str:
    return validate_issue_key(key).rsplit("-", 1)[0]


def _adf(value: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": value}]}],
    }


def _ensure_limit(limit: int, maximum: int = 100) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise JiraInputError(f"limit must be 1-{maximum}")


def _detail(
    data: dict, comments: list[dict] | None = None, attachments: list[dict] | None = None
) -> dict:
    result = issue_fields(data)
    reporter = (data.get("fields") or {}).get("reporter") or {}
    result["reported_by"] = reporter.get("displayName")
    result["description"] = adf_text((data.get("fields") or {}).get("description"))
    result["comments"] = comments or []
    result["attachments"] = attachments or []
    return result


@tool(
    guidelines=("Fetch issue details before requesting comments.",),
    native=False,
    exec_docs=False,
    namespace="jira",
)
async def get_issue(issue_key: str) -> dict:
    """Get bounded issue details without comments."""
    key = validate_issue_key(issue_key)
    require_read(_issue_project(key))
    async with jira_client() as client:
        data = await client.request("GET", f"/issue/{key}?fields=*all")
    if not isinstance(data, dict) or not data.get("key") or not data.get("id"):
        raise JiraResponseError("Jira returned malformed issue details")
    fields = data.get("fields") or {}
    comments = (
        fields.get("comment", {}).get("comments")
        if isinstance(fields.get("comment"), dict)
        else None
    )
    attachments = fields.get("attachment")
    async with jira_client() as client:
        if not isinstance(comments, list):
            comment_data = await client.request(
                "GET", f"/issue/{key}/comment?startAt=0&maxResults=50&orderBy=-created"
            )
            comments = comment_data.get("comments", []) if isinstance(comment_data, dict) else []
        if not isinstance(attachments, list):
            attachment_data = await client.request("GET", f"/issue/{key}?fields=attachment")
            attachments = (
                (attachment_data.get("fields") or {}).get("attachment", [])
                if isinstance(attachment_data, dict)
                else []
            )
    normalized_comments = [item for item in comments if isinstance(item, dict)]
    normalized_comments.sort(key=lambda item: item.get("created") or "", reverse=True)
    return _detail(
        data,
        [comment(item) for item in normalized_comments[:50]],
        [attachment(item) for item in attachments if isinstance(item, dict)],
    )


@tool(native=False, exec_docs=False, namespace="jira")
async def search_users(query: str, limit: int = 20) -> list[dict]:
    """Find bounded active Jira user candidates without choosing one."""
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise JiraInputError("query must be 1-200 characters")
    _ensure_limit(limit, 50)
    require_read()
    result, start = [], 0
    async with jira_client() as client:
        while len(result) < limit:
            params = {"query": query, "startAt": start, "maxResults": min(50, limit - len(result))}
            data = await client.request("GET", f"/user/assignable/search?{urlencode(params)}")
            if not isinstance(data, list):
                raise JiraResponseError("Jira returned malformed users")
            candidates = [x for x in data if isinstance(x, dict) and x.get("accountType") != "app"]
            result.extend(
                {
                    "account_id": x.get("accountId"),
                    "display_name": str(x.get("displayName", ""))[:200],
                    "email": str(x.get("emailAddress", ""))[:320],
                }
                for x in candidates
            )
            if len(data) < params["maxResults"]:
                break
            start += len(data)
    return result[:limit]


async def _resolve_assignee(client: Any, value: str | None, project: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise JiraInputError("assignee must be 1-200 characters")
    require_write(project)
    params = urlencode({"query": value, "maxResults": 50})
    data = await client.request("GET", f"/user/assignable/search?{params}")
    if not isinstance(data, list):
        raise JiraResponseError("Jira returned malformed assignee candidates")
    candidates = [
        item for item in data if isinstance(item, dict) and item.get("accountType") != "app"
    ]
    exact = [
        item
        for item in candidates
        if value.casefold()
        in {
            str(item.get("accountId", "")).casefold(),
            str(item.get("displayName", "")).casefold(),
            str(item.get("name", "")).casefold(),
            str(item.get("username", "")).casefold(),
            str(item.get("emailAddress", "")).casefold(),
        }
    ]
    if len(exact) != 1:
        raise JiraInputError("assignee is unavailable or ambiguous")
    if not exact[0].get("accountId"):
        raise JiraResponseError("Jira returned malformed assignee candidate")
    return {"accountId": exact[0]["accountId"]}


async def _mutate_issue(issue_key: str, method: str, path: str, **kwargs: Any) -> dict:
    key = validate_issue_key(issue_key)
    require_write(_issue_project(key))
    async with jira_client() as client:
        try:
            await client.request(method, path, **kwargs)
        except JiraTransportError as exc:
            if exc.transmitted:
                raise JiraMutationIndeterminateError(
                    f"Mutation for {key} may have succeeded; verify with jira.get_issue"
                ) from exc
            raise
        try:
            data = await client.request("GET", f"/issue/{key}")
        except Exception as exc:
            raise JiraMutationIndeterminateError(
                f"Mutation for {key} may have succeeded; verify with jira.get_issue"
            ) from exc
    if not isinstance(data, dict):
        raise JiraMutationIndeterminateError(
            f"Mutation for {key} may have succeeded; verify with jira.get_issue"
        )
    return _detail(data)


@tool(native=False, exec_docs=False, namespace="jira")
async def create_issue(
    project: str,
    summary: str,
    type: str = "Task",
    description: str | None = None,
    labels: list[str] | None = None,
    status: str | None = None,
    assignee: str | None = None,
    parent: str | None = None,
    priority: str | None = None,
) -> dict:
    """Create an issue, optionally applying an exact post-create transition."""
    key = project_key(project)
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= _MAX_TEXT:
        raise JiraInputError("summary must be 1-8000 characters")
    if not isinstance(type, str) or not type.strip():
        raise JiraInputError("type must not be empty")
    if description is not None and (
        not isinstance(description, str) or len(description) > _MAX_TEXT
    ):
        raise JiraInputError("description must be at most 8000 characters")
    if status is not None and (not isinstance(status, str) or not status.strip()):
        raise JiraInputError("status must not be empty")
    require_write(key)
    payload = {
        "fields": {
            "project": {"key": key},
            "summary": summary,
            "issuetype": {"name": type},
            **({"description": _adf(description)} if description else {}),
            **({"labels": labels} if labels is not None else {}),
            **({"parent": {"key": validate_issue_key(parent)}} if parent else {}),
            **({"priority": {"name": priority}} if priority else {}),
        }
    }
    async with jira_client() as client:
        resolved_assignee = await _resolve_assignee(client, assignee, key)
        if resolved_assignee:
            payload["fields"]["assignee"] = resolved_assignee
        try:
            data = await client.request("POST", "/issue", json=payload)
        except Exception:
            raise
        created = data.get("key") if isinstance(data, dict) else None
        if not created:
            raise JiraResponseError("Jira returned malformed issue creation response")
        if status is None:
            return {"key": created, "id": data.get("id")}
        try:
            available = await client.request("GET", f"/issue/{created}/transitions")
            matches = [
                item
                for item in available.get("transitions", [])
                if item.get("name", "").casefold() == status.casefold()
            ]
            if len(matches) != 1:
                raise JiraInputError("Transition is unavailable or ambiguous")
            await client.request(
                "POST",
                f"/issue/{created}/transitions",
                json={"transition": {"id": matches[0]["id"]}},
            )
            result = await client.request("GET", f"/issue/{created}")
        except JiraInputError:
            raise JiraMutationIndeterminateError(
                f"Issue {created} was created but requested status was not applied; verify with jira.get_issue"
            ) from None
        except Exception as exc:
            raise JiraMutationIndeterminateError(
                f"Issue {created} was created but follow-up status is indeterminate; verify with jira.get_issue"
            ) from exc
    if not isinstance(result, dict):
        raise JiraMutationIndeterminateError(
            f"Issue {created} was created but verification failed; verify with jira.get_issue"
        )
    return _detail(result)


@tool(native=False, exec_docs=False, namespace="jira")
async def update_issue(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
    status: str | None = None,
    assignee: str | None = None,
    parent: str | None = None,
    priority: str | None = None,
) -> dict:
    """Update explicitly supplied issue fields."""
    if (
        summary is None
        and description is None
        and labels is None
        and status is None
        and assignee is None
        and parent is None
        and priority is None
    ):
        raise JiraInputError("at least one field is required")
    if summary is not None and (
        not isinstance(summary, str) or not summary.strip() or len(summary) > _MAX_TEXT
    ):
        raise JiraInputError("summary must be 1-8000 characters")
    if status is not None and (not isinstance(status, str) or not status.strip()):
        raise JiraInputError("status must not be empty")
    if description is not None and (
        not isinstance(description, str) or len(description) > _MAX_TEXT
    ):
        raise JiraInputError("description must be at most 8000 characters")
    if assignee is not None and not isinstance(assignee, str):
        raise JiraInputError("assignee must be a string")
    if labels is not None and (
        not isinstance(labels, list) or any(not isinstance(x, str) for x in labels)
    ):
        raise JiraInputError("labels must be a list of strings")
    key = validate_issue_key(issue_key)
    fields = {}
    if summary is not None:
        fields["summary"] = summary[:_MAX_TEXT]
    if description is not None:
        fields["description"] = _adf(description) if description else None
    if labels is not None:
        fields["labels"] = labels
    if assignee is not None:
        if assignee:
            async with jira_client() as client:
                fields["assignee"] = await _resolve_assignee(client, assignee, _issue_project(key))
        else:
            fields["assignee"] = None
    if parent is not None:
        fields["parent"] = {"key": validate_issue_key(parent)} if parent else None
    if priority is not None:
        fields["priority"] = {"name": priority} if priority else None
    result = (
        await _mutate_issue(key, "PUT", f"/issue/{key}", json={"fields": fields})
        if fields
        else None
    )
    if status is not None:
        result = await transition_issue(key, status)
    if result is None:
        raise JiraInputError("at least one field is required")
    return result


@tool(native=False, exec_docs=False, namespace="jira")
async def transition_issue(issue_key: str, status: str) -> dict:
    """Transition an issue after exact status discovery."""
    key = validate_issue_key(issue_key)
    require_write(_issue_project(key))
    if not isinstance(status, str) or not status.strip():
        raise JiraInputError("status must not be empty")
    async with jira_client() as client:
        available = await client.request("GET", f"/issue/{key}/transitions")
        if not isinstance(available, dict) or not isinstance(available.get("transitions"), list):
            raise JiraResponseError("Jira returned malformed transitions")
        matches = [
            item
            for item in available["transitions"]
            if item.get("name", "").casefold() == status.casefold()
        ]
        if len(matches) != 1:
            raise JiraInputError("Transition is unavailable or ambiguous")
        try:
            await client.request(
                "POST", f"/issue/{key}/transitions", json={"transition": {"id": matches[0]["id"]}}
            )
            result = await client.request("GET", f"/issue/{key}")
        except Exception as exc:
            raise JiraMutationIndeterminateError(
                f"Transition for {key} may have succeeded; verify with jira.get_issue"
            ) from exc
    if not isinstance(result, dict):
        raise JiraMutationIndeterminateError(
            f"Transition for {key} is indeterminate; verify with jira.get_issue"
        )
    return _detail(result)


@tool(native=False, exec_docs=False, namespace="jira")
async def add_comment(issue_key: str, body: str) -> dict:
    """Add an explicitly requested bounded comment."""
    key = validate_issue_key(issue_key)
    if not isinstance(body, str) or not 1 <= len(body) <= _MAX_TEXT:
        raise JiraInputError("comment body must be 1-8000 characters")
    require_write(_issue_project(key))
    async with jira_client() as client:
        try:
            data = await client.request("POST", f"/issue/{key}/comment", json={"body": _adf(body)})
        except JiraTransportError as exc:
            if exc.transmitted:
                raise JiraMutationIndeterminateError(
                    f"Comment on {key} may have succeeded; verify with jira.get_issue"
                ) from exc
            raise
    if not isinstance(data, dict) or not data.get("id"):
        raise JiraMutationIndeterminateError(
            f"Comment on {key} is indeterminate; verify with jira.get_issue"
        )
    return {
        "id": str(data["id"]),
        "author": (data.get("author") or {}).get("displayName"),
        "body": adf_text(data.get("body")),
        "created": gmt(data.get("created")),
    }
