"""Workspace-confined Jira attachments."""

from __future__ import annotations

import os
from pathlib import Path

from archie_agent.exec.tools import ToolError, tool

from .client import JiraResponseError, JiraTransportError, jira_client
from .issues import JiraMutationIndeterminateError
from .policy import issue_key as validate_issue_key
from .policy import require_write

_ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".txt", ".csv", ".json", ".zip"}
_MAX = 10 * 1024 * 1024


class JiraAttachmentError(ToolError):
    pass


def _open_workspace_file(workspace: Path, candidate: Path) -> tuple[int, str]:
    relative = candidate.relative_to(workspace)
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise JiraAttachmentError("Attachment path is not allowed")
    opened: list[int] = []
    try:
        current_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
        opened.append(current_fd)
        for component in parts[:-1]:
            current_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd
            )
            opened.append(current_fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        return file_fd, parts[-1]
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass


@tool(
    guidelines=("Use Jira only through the `jira` namespace and never provide credentials.",),
    native=False,
    exec_docs=False,
    namespace="jira",
)
async def attach_file(issue_key: str, path: str) -> dict:
    """Attach one bounded, allowed file from the workspace."""
    key = validate_issue_key(issue_key)
    require_write(key.rsplit("-", 1)[0])
    workspace = Path("/workspace").resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(workspace)
        if resolved.suffix.lower() not in _ALLOWED:
            raise JiraAttachmentError("Attachment path or type is not allowed")
        fd, filename = _open_workspace_file(workspace, resolved)
        with os.fdopen(fd, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > _MAX:
                raise JiraAttachmentError("Attachment exceeds 10 MiB")
            content = handle.read(_MAX + 1)
    except JiraAttachmentError:
        raise
    except (FileNotFoundError, IsADirectoryError, OSError, ValueError) as exc:
        raise JiraAttachmentError("Attachment file is unavailable") from exc
    if len(content) > _MAX:
        raise JiraAttachmentError("Attachment exceeds 10 MiB")
    async with jira_client() as client:
        try:
            data = await client.request(
                "POST",
                f"/issue/{key}/attachments",
                files={"file": (filename, content)},
                headers={"X-Atlassian-Token": "no-check"},
            )
        except JiraTransportError as exc:
            if exc.transmitted:
                raise JiraMutationIndeterminateError(
                    f"Attachment on {key} may have succeeded; verify with jira.get_issue"
                ) from exc
            raise
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise JiraResponseError("Jira returned malformed attachment metadata")
    item = data[0]
    if not item.get("id") or not item.get("filename"):
        raise JiraResponseError("Jira returned malformed attachment metadata")
    return {
        "id": item.get("id"),
        "filename": item.get("filename"),
        "size": item.get("size"),
        "content_url": item.get("content"),
    }
