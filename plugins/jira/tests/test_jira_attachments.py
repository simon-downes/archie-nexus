"""Jira attachments are confined to the context workspace (070 M9)."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.jira.attachments import JiraAttachmentError, attach_file

WRITE = {"jira": {"write": {"enabled": True, "scope": ["PLAT"]}}}
UPLOADED = [{"id": "9", "filename": "report.txt", "size": 5, "content": "https://x/9"}]


async def test_attach_reads_from_context_workspace(policy, tmp_path):
    (tmp_path / "report.txt").write_bytes(b"hello")
    policy(WRITE, workspace=tmp_path)
    client = AsyncMock()
    client.request.return_value = UPLOADED
    with patch("archie_plugins.jira.attachments.jira_client") as factory:
        factory.return_value.__aenter__.return_value = client
        result = await attach_file("PLAT-1", "report.txt")
    assert result == {"id": "9", "filename": "report.txt", "size": 5, "content_url": "https://x/9"}
    assert client.request.call_args.kwargs["files"] == {"file": ("report.txt", b"hello")}


async def test_attach_rejects_path_outside_context_workspace(policy, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    policy(WRITE, workspace=workspace)
    with patch("archie_plugins.jira.attachments.jira_client") as factory:
        with pytest.raises(JiraAttachmentError, match="unavailable|not allowed"):
            await attach_file("PLAT-1", str(tmp_path / "secret.txt"))
        factory.assert_not_called()


async def test_attach_without_workspace_fails_closed(policy, tmp_path):
    (tmp_path / "report.txt").write_bytes(b"hello")
    policy(WRITE, workspace=None)
    with patch("archie_plugins.jira.attachments.jira_client") as factory:
        with pytest.raises(JiraAttachmentError, match="^Attachment path is not allowed$"):
            await attach_file("PLAT-1", str(tmp_path / "report.txt"))
        factory.assert_not_called()


async def test_attach_requires_write_policy_before_workspace(policy, tmp_path):
    policy({}, workspace=None)
    with pytest.raises(Exception, match="Jira writes are disabled by policy"):
        await attach_file("PLAT-1", "report.txt")
