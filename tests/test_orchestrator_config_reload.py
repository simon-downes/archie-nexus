"""The orchestrator re-reads config.yaml on use instead of caching it at startup."""

import os
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.auth import AuthService
from archie_orchestrator.config_source import ConfigSource, config_source
from archie_orchestrator.credential_refresh import refresh_expiring_credentials
from archie_shared.config import ConfigError, home_dir
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import replace_credential
from archie_shared.schemas import load_nexus_config
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

SCALR = "plugins:\n  scalr:\n    settings: {hostname: %s, account: acct}\n"


def _write(text: str) -> None:
    path = home_dir() / "config.yaml"
    path.write_text(text)
    # Bump mtime so back-to-back writes in one test are never seen as unchanged.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_missing_file_yields_defaults():
    assert ConfigSource().get().plugins == {}


def test_get_reloads_after_edit():
    source = ConfigSource()
    _write(SCALR % "one.example")
    assert source.get().plugins["scalr"].settings["hostname"] == "one.example"
    _write(SCALR % "two.example")
    assert source.get().plugins["scalr"].settings["hostname"] == "two.example"


def test_get_caches_until_the_file_changes():
    source = ConfigSource()
    _write(SCALR % "one.example")
    with patch(
        "archie_orchestrator.config_source.load_nexus_config", wraps=load_nexus_config
    ) as loader:
        source.get()
        source.get()
        assert loader.call_count == 1
        _write(SCALR % "two.example")
        source.get()
        assert loader.call_count == 2


def test_get_raises_on_invalid_file():
    source = ConfigSource()
    _write("plugins:\n  nope: {}\n")
    with pytest.raises(ConfigError):
        source.get()


def test_latest_keeps_last_valid_config_when_file_breaks(caplog):
    source = ConfigSource()
    _write(SCALR % "good.example")
    source.get()
    _write("plugins:\n  nope: {}\n")
    assert source.latest().plugins["scalr"].settings["hostname"] == "good.example"
    assert source.latest().plugins["scalr"].settings["hostname"] == "good.example"
    warnings = [r for r in caplog.records if "last valid config" in r.getMessage()]
    assert len(warnings) == 1  # one warning per invalid file version
    _write(SCALR % "fixed.example")
    assert source.latest().plugins["scalr"].settings["hostname"] == "fixed.example"


def test_latest_raises_without_any_valid_config():
    _write("plugins:\n  nope: {}\n")
    with pytest.raises(ConfigError):
        ConfigSource().latest()


def test_runtime_environment_sees_settings_added_after_first_load():
    """The Scalr case: settings added after the orchestrator first read the config."""
    replace_credential("scalr", {"token": "tok"})
    source = ConfigSource()
    _write("plugins: {}\n")
    assert "SCALR_HOSTNAME" not in runtime_environment(source.get())
    _write(SCALR % "tillo.example")
    env = runtime_environment(source.get())
    assert env["SCALR_HOSTNAME"] == "tillo.example"
    assert env["SCALR_ACCOUNT"] == "acct"


def test_auth_service_follows_config_edits():
    service = AuthService(ConfigSource().latest)
    _write("plugins:\n  datadog: {enabled: false}\n")
    assert "datadog" not in service.provider_names()
    _write("plugins: {}\n")
    assert "datadog" in service.provider_names()


def test_auth_service_accepts_a_fixed_config():
    service = AuthService(ConfigSource().get())
    assert "datadog" in service.provider_names()


@pytest.mark.asyncio
async def test_credential_refresh_reads_current_enabled_set(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from archie_shared.credentials.models import OAuthCredential

    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiring = OAuthCredential(
        access_token="old", refresh_token="r", expires_at=(now + timedelta(minutes=1)).isoformat()
    )
    monkeypatch.setattr(
        "archie_orchestrator.credential_refresh.get_credential",
        lambda name, credential_type: expiring if name == "slack" else None,
    )
    service = AuthService(ConfigSource().latest)
    refreshed: list[str] = []

    async def fake_refresh(name):
        refreshed.append(name)

    monkeypatch.setattr(service, "refresh", fake_refresh)
    _write("plugins:\n  slack: {enabled: false}\n")
    assert await refresh_expiring_credentials(service, now=now) == []
    _write("plugins: {}\n")
    assert await refresh_expiring_credentials(service, now=now) == ["slack"]


@pytest.mark.asyncio
async def test_post_sessions_uses_config_as_of_the_request():
    seen = []
    descriptor = SessionDescriptor(
        session_id="ws-01", container_name="archie-ws-01", port=1, raw_docker_status="Up"
    )

    def fake_start(workspace, config, *, backend="native"):
        seen.append(config.plugins["scalr"].settings["hostname"])
        return descriptor

    with patch("archie_orchestrator.app.start_session", side_effect=fake_start):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            _write(SCALR % "first.example")
            assert (await http.post("/sessions", json={"workspace": "ws"})).status_code == 200
            _write(SCALR % "second.example")
            assert (await http.post("/sessions", json={"workspace": "ws"})).status_code == 200
    assert seen == ["first.example", "second.example"]


@pytest.mark.asyncio
async def test_post_sessions_reports_invalid_config():
    _write("plugins:\n  nope: {}\n")
    with patch("archie_orchestrator.app.start_session") as start:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post("/sessions", json={"workspace": "ws"})
    assert response.status_code == 500
    assert "nope" in response.json()["error"]
    start.assert_not_called()


def test_module_source_resolves_home_per_call():
    """The shared instance follows ARCHIE_HOME_DIR, so per-test homes stay isolated."""
    _write(SCALR % "isolated.example")
    assert config_source.get().plugins["scalr"].settings["hostname"] == "isolated.example"
