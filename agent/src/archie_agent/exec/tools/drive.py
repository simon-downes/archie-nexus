"""Bounded Google Drive read tools and private reference seams."""

from __future__ import annotations

import builtins
from typing import Any

from archie_agent.exec.tools import tool
from archie_agent.exec.tools.google_common import (
    GoogleNotFoundError,
    GoogleValidationError,
    bounded_limit,
    clean,
    credential,
    google_json,
    google_text,
    require_read,
    safe_id,
    safe_url,
    text,
)


async def drive_request(operation: str, payload: dict[str, Any], token: str) -> Any:
    if operation in {"export", "download"}:
        file_id = payload["id"]
        mime_type = payload.get("mime_type")
        if operation == "export":
            export_mime = (
                "text/csv"
                if mime_type == "application/vnd.google-apps.spreadsheet"
                else "text/plain"
            )
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
            return await google_text(url, token, params={"mimeType": export_mime})
        return await google_text(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            token,
            params={"alt": "media"},
        )
    if operation in {"search", "list"}:
        data = await google_json("https://www.googleapis.com/drive/v3/files", token, params=payload)
        return data.get("files", [])
    return await google_json(
        f"https://www.googleapis.com/drive/v3/files/{payload['id']}",
        token,
        params={"fields": "id,name,mimeType,modifiedTime,webViewLink,trashed,parents"},
    )


def _metadata(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        file_id = safe_id(raw.get("id"), "file_id")
    except Exception:
        return None
    return {
        "id": file_id,
        "name": clean(raw.get("name"), 500),
        "mime_type": clean(raw.get("mimeType"), 256),
        "modified_time": clean(raw.get("modifiedTime"), 64),
        "web_url": safe_url(raw.get("webViewLink")),
        "parents": [clean(value, 256) for value in raw.get("parents", []) if clean(value, 256)][:10]
        if isinstance(raw.get("parents"), builtins.list)
        else [],
        "trashed": bool(raw.get("trashed", False)),
        "provenance": "google.drive",
    }


def _normalize(raw: Any, limit: int) -> list[dict[str, Any]]:
    return [
        item
        for item in (_metadata(value) for value in (raw if isinstance(raw, builtins.list) else []))
        if item
    ][:limit]


async def search_references(
    query: str, limit: int, policy_snapshot: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    query = text(query, "query")
    limit = bounded_limit(limit, maximum=50)
    require_read(policy_snapshot)
    raw = await drive_request(
        "search", {"q": query, "pageSize": limit}, credential().access_token or ""
    )
    return _normalize(raw, limit)


async def read_reference(
    file_id: str, purpose: str, policy_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    file_id = safe_id(file_id, "file_id")
    purpose = text(purpose, "purpose", maximum=128)
    require_read(policy_snapshot)
    result = _metadata(
        await drive_request(
            "metadata", {"id": file_id, "purpose": purpose}, credential().access_token or ""
        )
    )
    if result is None:
        raise GoogleNotFoundError("Drive file was not found")
    mime_type = result.get("mime_type")
    exportable = {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.presentation",
        "application/vnd.google-apps.spreadsheet",
    }
    textual = {"text/plain", "text/markdown", "text/csv", "application/json", "application/xml"}
    file_result = {key: value for key, value in result.items() if key != "parents"}
    envelope = {
        "file": file_result,
        "folder_id": result.get("parents", [None])[0] if result.get("parents") else None,
        "format": "reference",
        "content": None,
        "truncated": False,
        "provenance": "google.drive",
    }
    if not purpose.startswith("read") or mime_type not in exportable | textual:
        return envelope
    requested = purpose.partition(":")[2] or "auto"
    if requested == "auto" and mime_type not in exportable | textual:
        return envelope
    if requested == "csv" and mime_type != "application/vnd.google-apps.spreadsheet":
        raise GoogleValidationError("CSV format requires a Google Sheet")
    token = credential().access_token or ""
    operation = "export" if mime_type in exportable else "download"
    content = await drive_request(operation, {"id": file_id, "mime_type": mime_type}, token)
    if not isinstance(content, str):
        return envelope
    content = content[:100000]
    envelope.update(
        {
            "format": "csv" if mime_type == "application/vnd.google-apps.spreadsheet" else "text",
            "content": content,
            "truncated": len(content) == 100000,
        }
    )
    return envelope


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="drive")
async def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    return await search_references(query, limit)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="drive")
async def list(limit: int = 100) -> list[dict[str, Any]]:
    limit = bounded_limit(limit, maximum=100)
    require_read()
    raw = await drive_request("list", {"pageSize": limit}, credential().access_token or "")
    return _normalize(raw, limit)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="drive")
async def metadata(file_id: str) -> dict[str, Any]:
    return await read_reference(file_id, "metadata")


async def read_content(
    file_id: str,
    format: str = "auto",
    policy_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if format not in {"auto", "text", "csv", "reference"}:
        raise ValueError("Unsupported Drive format")
    purpose = "read" if format in {"auto", "text"} else f"read:{format}"
    return await read_reference(file_id, purpose, policy_snapshot)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="drive")
async def read(file_id: str, format: str = "auto") -> dict[str, Any]:
    return await read_content(file_id, format)


__all__ = ["search", "list", "metadata", "read"]
