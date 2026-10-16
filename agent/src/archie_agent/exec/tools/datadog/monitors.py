"""Monitor reads, validation and ownership-guarded writes.

Writes need ``tools.datadog.write.enabled``. Creates always carry ``managedby:archie``; updates
are refused unless the monitor already carries it. There is no delete.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from archie_agent.exec.tools import tool

from . import validation
from .client import (
    DatadogInputError,
    DatadogMutationIndeterminateError,
    DatadogOwnershipError,
    DatadogResponseError,
    DatadogTransportError,
    datadog_client,
)
from .formatting import has_ownership_tag, monitor
from .policy import require_read, require_write


def definition(
    name: Any,
    type: Any,
    query: Any,
    message: Any,
    tags: Any,
    priority: Any,
    options: Any,
) -> dict[str, Any]:
    """Validate a full monitor definition and return the request body, ownership tag included."""
    body: dict[str, Any] = {
        "name": validation.text(name, "name", 500),
        "type": validation.choice(type, "type", validation.MONITOR_TYPES),
        "query": validation.query(query),
        "message": validation.text(message, "message"),
        "tags": validation.with_ownership(validation.tags(tags)),
    }
    level = validation.priority(priority)
    if level is not None:
        body["priority"] = level
    extra = validation.monitor_options(options)
    if extra is not None:
        body["options"] = extra
    return body


@tool(native=False, exec_docs=False, namespace="datadog")
async def search_monitors(query: str | None = None, limit: int = 20) -> list[dict]:
    """Search monitors with monitor search syntax (for example ``status:alert tag:env:prod``)."""
    text = validation.optional_query(query)
    count = validation.limit(limit)
    require_read()
    params: dict[str, Any] = {"per_page": count, "page": 0}
    if text:
        params["query"] = text
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/monitor/search", params=params)
    items = data.get("monitors") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed monitor search response")
    return [monitor(item) for item in items[:count] if isinstance(item, dict)]


async def _fetch(client: Any, monitor_id: int) -> dict[str, Any]:
    data = await client.request("GET", f"/api/v1/monitor/{monitor_id}")
    if not isinstance(data, dict) or "id" not in data:
        raise DatadogResponseError("Datadog returned a malformed monitor")
    return data


@tool(native=False, exec_docs=False, namespace="datadog")
async def get_monitor(monitor_id: int) -> dict:
    """Return one monitor with its message and allowed options."""
    identifier = validation.monitor_id(monitor_id)
    require_read()
    async with datadog_client() as client:
        data = await _fetch(client, identifier)
    return monitor(data, detail=True)


@tool(native=False, exec_docs=False, namespace="datadog")
async def validate_monitor(
    name: str,
    type: str,
    query: str,
    message: str,
    tags: list[str] | None = None,
    priority: int | None = None,
    options: dict | None = None,
) -> dict:
    """Ask Datadog whether a monitor definition is valid, without creating it."""
    body = definition(name, type, query, message, tags, priority, options)
    require_read()
    async with datadog_client() as client:
        await client.request("POST", "/api/v1/monitor/validate", json=body)
    return {"valid": True}


def _indeterminate(action: str, target: str) -> DatadogMutationIndeterminateError:
    return DatadogMutationIndeterminateError(
        f"Monitor {action} for {target} may have succeeded; verify with "
        "datadog.get_monitor or datadog.search_monitors('tag:managedby:archie') before retrying"
    )


class _Write:
    """Tracks whether a write request has been handed to the transport."""

    def __init__(self, client: Any, action: str, target: str):
        self.client = client
        self.action = action
        self.target = target
        self.sent = False

    async def send(self, method: str, path: str, body: dict) -> Any:
        """Send the write. Failures after it may have reached Datadog are indeterminate.

        ``sent`` stays True unless the transport proved nothing left (a connect failure), so
        the overall deadline expiring mid-request is reported as indeterminate.
        """
        self.sent = True
        try:
            return await self.client.request(method, path, json=body)
        except DatadogTransportError as exc:
            if exc.transmitted:
                raise _indeterminate(self.action, self.target) from exc
            self.sent = False
            raise
        except DatadogResponseError as exc:
            raise _indeterminate(self.action, self.target) from exc


@asynccontextmanager
async def _write_client(action: str, target: str):
    """Yield a ``_Write``; the overall deadline expiring after the write was sent is indeterminate."""
    write: _Write | None = None
    try:
        async with datadog_client() as client:
            write = _Write(client, action, target)
            yield write
    except DatadogTransportError as exc:
        if write is not None and write.sent:
            raise _indeterminate(action, target) from exc
        raise


@tool(native=False, exec_docs=False, namespace="datadog")
async def create_monitor(
    name: str,
    type: str,
    query: str,
    message: str,
    tags: list[str] | None = None,
    priority: int | None = None,
    options: dict | None = None,
) -> dict:
    """Create a monitor tagged ``managedby:archie``. Needs write policy."""
    body = definition(name, type, query, message, tags, priority, options)
    require_write()
    async with _write_client("create", repr(body["name"])) as write:
        data = await write.send("POST", "/api/v1/monitor", body)
    if not isinstance(data, dict) or "id" not in data:
        raise _indeterminate("create", repr(body["name"]))
    return monitor(data, detail=True)


@tool(native=False, exec_docs=False, namespace="datadog")
async def update_monitor(
    monitor_id: int,
    name: str | None = None,
    query: str | None = None,
    message: str | None = None,
    tags: list[str] | None = None,
    priority: int | None = None,
    options: dict | None = None,
) -> dict:
    """Update a monitor tagged ``managedby:archie``; other monitors are refused. Needs write policy."""
    identifier = validation.monitor_id(monitor_id)
    changes: dict[str, Any] = {}
    if name is not None:
        changes["name"] = validation.text(name, "name", 500)
    if query is not None:
        changes["query"] = validation.query(query)
    if message is not None:
        changes["message"] = validation.text(message, "message")
    if tags is not None:
        changes["tags"] = validation.with_ownership(validation.tags(tags))
    if priority is not None:
        changes["priority"] = validation.priority(priority)
    extra = validation.monitor_options(options)
    if not changes and not extra:
        raise DatadogInputError("update_monitor needs at least one field to change")
    require_write()
    target = f"monitor {identifier}"
    async with _write_client("update", target) as write:
        current = await _fetch(write.client, identifier)
        if not has_ownership_tag(current.get("tags")):
            raise DatadogOwnershipError(
                f"Monitor {identifier} is not tagged {validation.OWNERSHIP_TAG}; "
                "only monitors created by this tool can be changed"
            )
        if extra:
            existing = current.get("options") if isinstance(current.get("options"), dict) else {}
            changes["options"] = {**existing, **extra}
        path = f"/api/v1/monitor/{identifier}"
        await write.send("PUT", path, changes)
        try:
            data = await _fetch(write.client, identifier)
        except Exception as exc:
            raise _indeterminate("update", target) from exc
    return monitor(data, detail=True)
