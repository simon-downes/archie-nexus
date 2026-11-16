"""The agent's local API on its Unix socket (plan 090)."""

import os
import stat
from pathlib import Path

import pytest
from archie_agent import host_config, local_api
from archie_agent.local_api import LocalApiError
from archie_shared.config import home_dir
from archie_shared.credentials.store import replace_credential
from starlette.testclient import TestClient


async def test_serves_credentials_and_settings_over_the_socket(monkeypatch):
    import asyncio

    (home_dir() / "config.yaml").write_text(
        "plugins:\n  github:\n    settings:\n      work_orgs: [tillo]\n"
    )
    replace_credential("datadog", {"api_key": "a", "app_key": "b"})
    path = local_api.socket_path()
    async with local_api.serve_local() as served:
        assert served == path
        assert os.environ["ARCHIE_AGENT_SOCKET"] == str(path)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        credential = await asyncio.to_thread(local_api.get_credential, "datadog")
        absent = await asyncio.to_thread(local_api.fetch, "/credentials/notion")
        settings = await asyncio.to_thread(local_api.fetch, "/plugins/github/settings")
        with pytest.raises(LocalApiError, match="HTTP 404 unknown provider 'nosuch'"):
            await asyncio.to_thread(local_api.fetch, "/credentials/nosuch")
        with pytest.raises(LocalApiError, match="HTTP 404 unknown plugin 'nosuch'"):
            await asyncio.to_thread(local_api.fetch, "/plugins/nosuch/settings")
    assert (credential.api_key, credential.app_key) == ("a", "b")
    assert absent == {"value": None}
    assert settings["work_orgs"] == ["tillo"]
    assert not path.exists()


async def test_reads_go_through_the_cache(monkeypatch):
    import asyncio

    replace_credential("datadog", {"api_key": "first", "app_key": "b"})
    async with local_api.serve_local():
        first = await asyncio.to_thread(local_api.get_credential, "datadog")
        replace_credential("datadog", {"api_key": "second", "app_key": "b"})
        held = await asyncio.to_thread(local_api.get_credential, "datadog")
        await asyncio.to_thread(host_config.current().sync)
        synced = await asyncio.to_thread(local_api.get_credential, "datadog")
    assert (first.api_key, held.api_key, synced.api_key) == ("first", "first", "second")


async def test_orchestrator_failure_is_503(monkeypatch):
    import asyncio

    host = host_config.current()
    monkeypatch.setattr(host, "_source", _Down())
    async with local_api.serve_local():
        with pytest.raises(LocalApiError, match="HTTP 503"):
            await asyncio.to_thread(local_api.fetch, "/credentials/datadog")


class _Down:
    def sync(self, request):
        raise host_config.HostConfigError("orchestrator down")


def test_the_agents_tcp_app_has_no_local_routes(monkeypatch):
    from unittest.mock import MagicMock, patch

    from archie_agent import app as app_module

    monkeypatch.setenv("ARCHIE_SESSION_ID", "test-session")
    with (
        patch.object(app_module, "create_llm_client", return_value=MagicMock()),
        TestClient(app_module.app) as agent,
    ):
        assert agent.get("/credentials/github").status_code == 404
        assert agent.get("/plugins/github/settings").status_code == 404
        # The lifespan served the socket for subprocesses meanwhile.
        assert Path(os.environ["ARCHIE_AGENT_SOCKET"]).is_socket()
