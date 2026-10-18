"""``plugins.<id>.enabled: false`` hides a plugin on every startup surface (M5, D9, D11)."""

import contextvars
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.runner import _build_namespace, run
from archie_agent.exec.tool import run_exec
from archie_agent.skills import discover_skills
from archie_agent.tool_policy_live import LiveToolPolicy, startup_runner_state
from archie_cli.auth import login
from archie_orchestrator.app import app
from archie_orchestrator.auth import AuthService, UnknownProviderError
from archie_orchestrator.credential_refresh import refresh_expiring_credentials
from archie_shared.config import home_dir
from archie_shared.credentials.models import OAuthCredential
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import replace_credential
from archie_shared.plugins import enabled_plugins, is_enabled, load_manifests
from archie_shared.schemas import load_nexus_config
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

DATADOG_OFF = "plugins:\n  datadog: {enabled: false}\n"


def _config(text: str):
    (home_dir() / "config.yaml").write_text(text)
    return load_nexus_config()


def test_enabled_helpers():
    config = _config(DATADOG_OFF)
    manifests = load_manifests()
    enabled = enabled_plugins(config.plugins, manifests)
    assert "datadog" not in enabled
    assert enabled == set(manifests) - {"datadog"}
    assert is_enabled("datadog", config) is False
    assert is_enabled("jira", config) is True
    assert is_enabled("bedrock", config) is True  # core provider, not a plugin


def test_startup_runner_state_covers_enabled_plugins_only():
    config = _config(DATADOG_OFF + "  github:\n    settings: {work_orgs: [acme]}\n")
    settings, enabled = startup_runner_state(config, load_manifests())
    assert enabled == sorted(set(load_manifests()) - {"datadog"})
    assert set(settings) == set(enabled)
    assert settings["github"]["work_orgs"] == ["acme"]


@pytest.mark.asyncio
async def test_auth_providers_and_status_omit_disabled(monkeypatch):
    config = _config(DATADOG_OFF)
    monkeypatch.setattr(app.state, "config", config, raising=False)
    monkeypatch.setattr(app.state, "auth_service", AuthService(config), raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        providers = (await http.get("/auth/providers")).json()
        statuses = (await http.get("/auth/status")).json()
        put = await http.put("/auth/credential/datadog", json={"api_key": "a", "app_key": "b"})
    names = {item["name"] for item in providers}
    assert "datadog" not in names and "jira" in names and "bedrock" in names
    assert "datadog" not in {item["provider"] for item in statuses}
    assert put.status_code == 404


def test_provider_lookup_rejects_disabled():
    service = AuthService(_config(DATADOG_OFF))
    with pytest.raises(UnknownProviderError):
        service.provider("datadog")
    assert "datadog" not in service.provider_names()
    assert service.provider("jira").name == "jira"


@pytest.mark.asyncio
async def test_credential_refresh_skips_disabled_oauth_plugin(monkeypatch):
    config = _config("plugins:\n  slack: {enabled: false}\n")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiring = OAuthCredential(
        access_token="old", refresh_token="r", expires_at=(now + timedelta(minutes=1)).isoformat()
    )
    monkeypatch.setattr(
        "archie_orchestrator.credential_refresh.get_credential",
        lambda name, credential_type: expiring if name in ("slack", "google") else None,
    )
    refreshed = []

    class Service:
        def __init__(self):
            self.config = config

        async def refresh(self, name):
            refreshed.append(name)

    assert await refresh_expiring_credentials(Service(), now=now) == ["google"]
    assert refreshed == ["google"]


def test_runtime_environment_skips_disabled_scalr():
    replace_credential("scalr", {"token": "tok"})
    config = _config(
        "plugins:\n  scalr:\n    enabled: false\n    settings: {hostname: h, account: a}\n"
    )
    env = runtime_environment(config)
    assert not {"SCALR_TOKEN", "SCALR_HOSTNAME", "SCALR_ACCOUNT"} & set(env)


def test_runtime_environment_projects_scalr_settings_when_enabled():
    replace_credential("scalr", {"token": "tok"})
    config = _config("plugins:\n  scalr:\n    settings: {hostname: h.example, account: acct}\n")
    env = runtime_environment(config)
    assert env["SCALR_TOKEN"] == "tok"
    assert env["SCALR_HOSTNAME"] == "h.example"
    assert env["SCALR_ACCOUNT"] == "acct"


def test_cli_login_refuses_disabled_plugin():
    _config(DATADOG_OFF)
    result = CliRunner().invoke(login, ["datadog"])
    assert result.exit_code != 0
    assert "datadog is disabled in config (plugins.datadog.enabled: false)" in result.output


@pytest.fixture
def no_user_skills(tmp_path, monkeypatch):
    """Only plugin skills: empty ~/.agents and persona dirs."""
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(tmp_path / "persona"))


def test_discover_skills_omits_disabled_plugin_skill(no_user_skills):
    assert "datadog" in discover_skills()
    _config(DATADOG_OFF)
    catalog = discover_skills()
    assert "datadog" not in catalog
    assert "jira" in catalog


def test_discover_skills_explicit_enabled_set(no_user_skills):
    catalog = discover_skills(["jira"])
    assert "jira" in catalog and "datadog" not in catalog and "slack" not in catalog


def test_discover_skills_invalid_config_includes_all(no_user_skills, caplog):
    (home_dir() / "config.yaml").write_text("plugins: [unclosed\n")
    catalog = discover_skills()
    assert "datadog" in catalog
    assert "including skills of every plugin" in caplog.text


def test_runner_imports_only_enabled_plugins():
    enabled = sorted(set(load_manifests()) - {"datadog"})
    namespace = contextvars.copy_context().run(
        _build_namespace, [], {"policy": {}, "settings": {}, "enabled": enabled}
    )
    assert "datadog" not in namespace
    full = contextvars.copy_context().run(
        _build_namespace, [], {"policy": {}, "enabled": sorted(load_manifests())}
    )
    assert "datadog" in full


def _plugin_namespaces(namespace):
    names = {ns for loaded in load_manifests().values() for ns in loaded.manifest.namespaces}
    return sorted(names & set(namespace))


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        [],
        "policy",
        {"policy": {}},  # enabled missing
        {"policy": {}, "enabled": None},
        {"policy": {}, "enabled": "datadog"},
    ],
)
def test_runner_context_without_enabled_list_loads_no_plugins(context):
    namespace = contextvars.copy_context().run(_build_namespace, [], context)
    assert _plugin_namespaces(namespace) == []
    assert callable(namespace["read"])


def test_runner_old_context_shape_is_empty_policy():
    namespace = contextvars.copy_context().run(
        _build_namespace, [], {"datadog": {"read": {"enabled": True}}}
    )
    assert namespace["_tool_policy"] == {}
    assert _plugin_namespaces(namespace) == []


@pytest.mark.parametrize("content", [None, "{not json", '["policy"]'])
def test_runner_missing_or_bad_context_file_loads_no_plugins(tmp_path, content):
    (tmp_path / "main.py").write_text(
        "async def main():\n    return sorted(k for k in ('datadog', 'jira') if k in globals())\n"
    )
    if content is not None:
        (tmp_path / "context.json").write_text(content)
    contextvars.copy_context().run(run, tmp_path)
    envelope = Envelope.read(tmp_path)
    assert envelope.ok, envelope.error
    assert envelope.return_value == []


def test_static_runner_context_lists_every_manifest():
    context = LiveToolPolicy.static({}).runner_context()
    assert context["enabled"] == sorted(load_manifests())
    assert "datadog" in context["enabled"]


READ_CONTEXT = (
    "import sys\n"
    "async def main():\n"
    "    return json.loads((Path(sys.argv[1]) / 'context.json').read_text())\n"
)


@pytest.mark.asyncio
async def test_run_exec_writes_runner_context(tmp_path):
    context = LiveToolPolicy(
        {"jira": {"read": {"enabled": True, "scope": None}}},
        loader=None,
        settings={"datadog": {"site": "datadoghq.com"}},
        enabled=["jira"],
    ).runner_context()
    envelope = await run_exec(
        READ_CONTEXT, run_root=tmp_path / "runs", python=sys.executable, runner_context=context
    )
    assert envelope.ok, envelope.error
    assert envelope.return_value == {
        "policy": {"jira": {"read": {"enabled": True, "scope": None}}},
        "settings": {"datadog": {"site": "datadoghq.com"}},
        "enabled": ["jira"],
    }
    assert json.loads(json.dumps(context)) == envelope.return_value
