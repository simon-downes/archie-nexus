"""Tests for POST /credentials endpoint."""

import os
import stat
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


@pytest.mark.asyncio
async def test_post_credentials_writes_file(tmp_path):
    """POST /credentials writes the body to ~/.nexus/credentials.yaml."""
    yaml_content = b"bedrock:\n  aws_access_key_id: AKIA123\n"

    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/credentials", content=yaml_content)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    cred_file = tmp_path / "credentials.yaml"
    assert cred_file.exists()
    assert cred_file.read_bytes() == yaml_content


@pytest.mark.asyncio
async def test_post_credentials_overwrites_existing(tmp_path):
    """POST /credentials over an existing file atomically replaces it."""
    old_content = b"old: data\n"
    new_content = b"new: credentials\n"

    cred_file = tmp_path / "credentials.yaml"
    cred_file.write_bytes(old_content)

    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/credentials", content=new_content)

    assert resp.status_code == 200
    assert cred_file.read_bytes() == new_content


@pytest.mark.asyncio
async def test_post_credentials_permissions_0600(tmp_path):
    """POST /credentials sets 0600 permissions on the written file."""
    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/credentials", content=b"key: value\n")

    cred_file = tmp_path / "credentials.yaml"
    mode = stat.S_IMODE(os.stat(cred_file).st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_post_credentials_empty_body_returns_400(tmp_path):
    """POST /credentials with empty body → 400."""
    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/credentials", content=b"")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_credentials_not_logged(tmp_path, caplog):
    """Credential file contents are never logged — only 'received' message."""
    import logging

    secret_content = b"bedrock:\n  aws_secret_access_key: SUPERSECRET123\n"

    with (
        patch("archie_orchestrator.app.home_dir", return_value=tmp_path),
        caplog.at_level(logging.DEBUG),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/credentials", content=secret_content)

    # The secret value should never appear in any log record
    all_log_text = " ".join(r.message for r in caplog.records)
    assert "SUPERSECRET123" not in all_log_text
    assert "Credentials received" in all_log_text
