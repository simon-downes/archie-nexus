"""The agent's host config: environment, HTTP source, cache and startup (plan 090)."""

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import msgspec
import pytest
from archie_agent import host_config
from archie_agent.host_config import HostConfig, HostConfigError, HttpSource
from archie_orchestrator.app import app as orchestrator_app
from archie_shared.agent_sync import SyncItem, SyncResponse, token_hash, version_of
from archie_shared.config import ConfigError, home_dir
from archie_shared.credentials.models import OAuthCredential
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import replace_credential
from archie_shared.plugins.config import PluginConfig
from archie_shared.schemas import NexusConfig
from starlette.testclient import TestClient

from tests.session_fakes import record, serve

TOKEN = "agent-token-for-tests"
LIVE = "proj-01khostcfg01"
MODEL = "bedrock-claude-sonnet-4-6"


def _container_env(monkeypatch, **values):
    monkeypatch.setenv("ARCHIE_AGENT_CONTAINER", "1")
    for name, value in values.items():
        monkeypatch.setenv(name, value)


class ScriptedSource:
    """Replies in order; an exception in the script is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def sync(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _config_reply(config: NexusConfig) -> SyncResponse:
    value = msgspec.to_builtins(config)
    return SyncResponse(config=SyncItem(version=version_of(value), value=value))


# ---------------------------------------------------------------------------
# source_from_env
# ---------------------------------------------------------------------------


def test_source_from_env_pops_the_token(monkeypatch, real_source_from_env):
    _container_env(
        monkeypatch,
        ARCHIE_ORCHESTRATOR_URL="http://host.docker.internal:7600",
        ARCHIE_AGENT_TOKEN=TOKEN,
    )
    source = real_source_from_env()
    assert isinstance(source, HttpSource)
    assert "ARCHIE_AGENT_TOKEN" not in os.environ
    from archie_agent.exec.tools._runtime_env import subprocess_env

    assert "ARCHIE_AGENT_TOKEN" not in subprocess_env()[0]


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({}, "ARCHIE_AGENT_CONTAINER"),
        ({"ARCHIE_AGENT_CONTAINER": "1", "ARCHIE_AGENT_TOKEN": TOKEN}, "ARCHIE_ORCHESTRATOR_URL"),
        (
            {"ARCHIE_AGENT_CONTAINER": "1", "ARCHIE_ORCHESTRATOR_URL": "http://h:1"},
            "ARCHIE_AGENT_TOKEN",
        ),
    ],
    ids=["no-flag", "no-url", "no-token"],
)
def test_source_from_env_names_what_is_missing(monkeypatch, real_source_from_env, env, missing):
    for name in ("ARCHIE_AGENT_CONTAINER", "ARCHIE_ORCHESTRATOR_URL", "ARCHIE_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(HostConfigError, match=missing):
        real_source_from_env()
    assert "ARCHIE_AGENT_TOKEN" not in os.environ


# ---------------------------------------------------------------------------
# HttpSource against the orchestrator's route
# ---------------------------------------------------------------------------


def _http_source(token: str = TOKEN) -> HttpSource:
    return HttpSource("http://testserver", token, client=TestClient(orchestrator_app))


def _live_token(tracker, token: str = TOKEN) -> None:
    tracker.store.insert(record(LIVE), agent_token_hash=token_hash(token))
    tracker._records[LIVE] = record(LIVE)


def test_http_source_starts_from_the_orchestrators_config():
    (home_dir() / "config.yaml").write_text(f"global:\n  model: {MODEL}\n")
    with serve(orchestrator_app) as tracker:
        _live_token(tracker)
        host = HostConfig(_http_source())
        host.start()
        assert host.config().global_.model == MODEL
        generation = host.generation
        host.sync()  # nothing changed
    assert host.generation == generation


def test_http_source_with_a_rejected_token_raises():
    with serve(orchestrator_app) as tracker:
        _live_token(tracker)
        with pytest.raises(HostConfigError, match="HTTP 401"):
            HostConfig(_http_source("wrong")).start()


def test_http_source_unreachable_raises():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(HostConfigError, match="can't reach the orchestrator"):
        HttpSource("http://host.docker.internal:7600", TOKEN, client=client).sync(
            host_config.SyncRequest()
        )


def test_http_source_unreadable_reply_raises():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="[")))
    with pytest.raises(HostConfigError, match="can't be read"):
        HttpSource("http://o", TOKEN, client=client).sync(host_config.SyncRequest())


# ---------------------------------------------------------------------------
# HostConfig
# ---------------------------------------------------------------------------


def test_start_with_an_invalid_host_config_raises_config_error():
    source = ScriptedSource(SyncResponse(config_error="Invalid YAML in config.yaml"))
    with pytest.raises(ConfigError, match="Invalid YAML"):
        HostConfig(source).start()


def test_config_error_keeps_the_config_until_a_good_sync_clears_it(caplog):
    good = NexusConfig()
    held = _config_reply(good).config.version
    source = ScriptedSource(
        _config_reply(good),
        SyncResponse(config_error="bad edit"),
        SyncResponse(config_error="bad edit"),
        SyncResponse(),  # restored to the held version: no item, no error
    )
    host = HostConfig(source)
    host.start()
    with caplog.at_level(logging.WARNING, logger="archie_agent.host_config"):
        host.sync()
        host.sync()
    assert host.config() == good
    assert host.config_error == "bad edit"
    assert caplog.text.count("bad edit") == 1  # warned once per error
    host.sync()
    assert host.config_error is None
    assert [r.config for r in source.requests] == [None, held, held, held]


def test_generation_moves_only_when_the_config_changes():
    first, second = NexusConfig(), msgspec.convert({"global": {"model": MODEL}}, NexusConfig)
    host = HostConfig(ScriptedSource(_config_reply(first), SyncResponse(), _config_reply(second)))
    host.start()
    start = host.generation
    host.sync()
    assert host.generation == start
    host.sync()
    assert host.generation == start + 1
    assert host.config().global_.model == MODEL


def test_failed_sync_raises_and_keeps_what_is_held():
    host = HostConfig(ScriptedSource(_config_reply(NexusConfig()), HostConfigError("down")))
    host.start()
    with pytest.raises(HostConfigError, match="down"):
        host.sync()
    assert host.config() == NexusConfig()


def test_current_builds_once_from_the_source_factory(monkeypatch):
    built = []

    def factory():
        built.append(1)
        return ScriptedSource(_config_reply(NexusConfig()))

    monkeypatch.setattr(host_config, "source_from_env", factory)
    assert host_config.current() is host_config.current()
    assert built == [1]


# ---------------------------------------------------------------------------
# Credentials (pull-through)
# ---------------------------------------------------------------------------


class CountingSource:
    """Answers from fixed config and store values; counts requests per credential."""

    def __init__(self, store=None, *, fail_after=None):
        self.store = dict(store or {})
        self.requests = []
        self.fail_after = fail_after

    def sync(self, request):
        from archie_orchestrator.agent_api import answer_sync

        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            raise HostConfigError("orchestrator down")
        self.requests.append(request)
        return answer_sync(request, load_config=NexusConfig, load_store=lambda: self.store)

    def asked_for(self, name):
        return [r for r in self.requests if name in r.credentials]


def _started(source, **kwargs) -> HostConfig:
    host = HostConfig(source, **kwargs)
    host.start()
    return host


def test_repeated_reads_make_one_request_including_for_absent_credentials():
    source = CountingSource({"github": {"personal_token": "p"}})
    host = _started(source)
    assert host.get_credential("github").personal_token == "p"
    assert host.get_credential("github").personal_token == "p"
    assert host.get_credential("notion") is None
    assert host.get_credential("notion") is None
    assert len(source.requests) == 3  # start, github miss, notion miss
    assert source.requests[1].credentials == {"github": None}


def test_a_sync_sends_held_versions_and_applies_changes():
    source = CountingSource({"github": {"personal_token": "old"}})
    host = _started(source)
    host.get_credential("github")
    generation = host.generation
    source.store["github"] = {"personal_token": "new"}
    host.sync()
    assert source.requests[-1].credentials == {"github": version_of({"personal_token": "old"})}
    assert host.get_credential("github").personal_token == "new"
    assert host.generation == generation + 1


def test_a_miss_the_orchestrator_cant_answer_raises():
    host = _started(CountingSource())
    with pytest.raises(HostConfigError, match="'nosuch' credential"):
        host.get_credential("nosuch", OAuthCredential)
    with pytest.raises(KeyError, match="Unknown service"):
        host.get_credential("nosuch")


def test_an_error_for_a_held_credential_keeps_it(caplog):
    source = CountingSource({"github": {"personal_token": "kept"}})
    host = _started(source)
    host.get_credential("github")
    source.store = {"github": "not a mapping"}  # a mid-edit credentials.yaml
    with caplog.at_level(logging.WARNING, logger="archie_agent.host_config"):
        host.sync()
    assert host.get_credential("github").personal_token == "kept"
    assert "Keeping the held github credential" in caplog.text


def test_an_entry_that_doesnt_fit_its_type_raises_value_error():
    host = _started(CountingSource({"github": {"bogus_field": "x"}}))
    with pytest.raises(ValueError, match="Validation error for 'github'"):
        host.get_credential("github")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def _oauth(expires_at: datetime) -> dict:
    return {"access_token": "a", "refresh_token": "r", "expires_at": expires_at.isoformat()}


def test_a_read_near_expiry_syncs_first():
    clock = Clock()
    source = CountingSource({"notion": _oauth(clock.now + timedelta(seconds=100))})
    host = _started(source, now=clock)
    host.get_credential("notion")  # miss
    source.store["notion"] = _oauth(clock.now + timedelta(hours=1))
    assert len(source.asked_for("notion")) == 1

    held = host.credential_entry("notion")  # 100 s before expiry: syncs first
    assert len(source.asked_for("notion")) == 2
    assert held["expires_at"] == (clock.now + timedelta(hours=1)).isoformat()


def test_early_syncs_are_spaced_and_failures_return_the_held_value(caplog):
    clock = Clock()
    expiring = _oauth(clock.now + timedelta(seconds=100))
    source = CountingSource({"notion": expiring})
    host = _started(source, now=clock)
    host.get_credential("notion")
    host.get_credential("notion")  # early sync, refresh not done on the host yet
    clock.now += timedelta(seconds=10)
    host.get_credential("notion")  # within 30 s of the last early sync: no request
    assert len(source.asked_for("notion")) == 2

    source.fail_after = len(source.requests)
    clock.now += timedelta(seconds=30)
    with caplog.at_level(logging.WARNING, logger="archie_agent.host_config"):
        assert host.credential_entry("notion") == expiring
    assert "Early sync for the notion credential failed" in caplog.text


def test_a_credential_far_from_expiry_or_without_one_never_syncs_early():
    clock = Clock()
    source = CountingSource(
        {"notion": _oauth(clock.now + timedelta(hours=1)), "github": {"personal_token": "p"}}
    )
    host = _started(source, now=clock)
    for _ in range(3):
        host.get_credential("notion")
        host.get_credential("github")
    assert len(source.requests) == 3  # start and the two misses


def test_refresh_syncs_the_named_credential(monkeypatch):
    source = CountingSource({"bedrock": {"aws_access_key_id": "A", "aws_secret_access_key": "S"}})
    host_config.install(_started(source))
    host_config.refresh("bedrock")
    assert source.requests[-1].credentials == {"bedrock": None}
    source.fail_after = len(source.requests)
    host_config.refresh("bedrock")  # a failure is logged, not raised


def test_runtime_environment_reads_through_the_lookup():
    replace_credential("scalr", {"token": "from-the-file"})
    source = CountingSource({"scalr": {"token": "from-the-orchestrator"}})
    host = _started(source)
    config = NexusConfig(
        plugins={"scalr": PluginConfig(settings={"hostname": "h", "account": "a"})}
    )
    env = runtime_environment(config, lookup=host.get_credential)
    assert env["SCALR_TOKEN"] == "from-the-orchestrator"


def test_bedrock_builds_from_and_refreshes_through_the_host_config():
    from archie_agent.llm.bedrock import BedrockClient

    source = CountingSource({"bedrock": {"aws_access_key_id": "A1", "aws_secret_access_key": "S"}})
    host_config.install(_started(source))
    with patch("archie_agent.llm.bedrock.boto3.client") as client:
        bedrock = BedrockClient("model", "eu-west-1")
        assert client.call_args.kwargs["aws_access_key_id"] == "A1"
        source.store["bedrock"] = {"aws_access_key_id": "A2", "aws_secret_access_key": "S"}
        assert bedrock._try_refresh_credentials()
        assert client.call_args.kwargs["aws_access_key_id"] == "A2"


def test_llm_and_kiro_credentials_come_from_the_host_config(monkeypatch):
    from archie_agent.kiro.harness import resolve_api_key
    from archie_agent.llm import openai_api

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    source = CountingSource({"openai": {"api_key": "sk-host"}, "kiro": {"api_key": "k-host"}})
    host_config.install(_started(source))
    assert openai_api._api_key() == "sk-host"
    assert resolve_api_key() == "k-host"


# ---------------------------------------------------------------------------
# Periodic sync
# ---------------------------------------------------------------------------


async def _run_loop(host, ticks: int = 3) -> None:
    task = asyncio.create_task(host_config.sync_loop(host, interval=0.01))
    await asyncio.sleep(0.01 * ticks + 0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_sync_loop_brings_host_edits_in():
    source = CountingSource({"github": {"personal_token": "old"}})
    host = _started(source)
    host.get_credential("github")
    source.store["github"] = {"personal_token": "new"}
    await _run_loop(host)
    assert host.get_credential("github").personal_token == "new"
    assert len(source.requests) > 2


async def test_sync_loop_keeps_values_and_runs_on_after_failures(caplog):
    source = CountingSource({"github": {"personal_token": "kept"}})
    host = _started(source)
    host.get_credential("github")
    source.fail_after = len(source.requests)
    with caplog.at_level(logging.WARNING, logger="archie_agent.host_config"):
        await _run_loop(host, ticks=4)
    assert host.get_credential("github").personal_token == "kept"
    assert caplog.text.count("Periodic sync failed") >= 2


async def test_sync_loop_survives_an_unexpected_error(caplog):
    host = _started(
        ScriptedSource(_config_reply(NexusConfig()), RuntimeError("boom"), *([SyncResponse()] * 50))
    )
    with caplog.at_level(logging.ERROR, logger="archie_agent.host_config"):
        await _run_loop(host, ticks=3)
    assert "Periodic sync failed unexpectedly" in caplog.text
    assert host.config() == NexusConfig()


def test_the_agent_syncs_while_it_runs(monkeypatch):
    from archie_agent import app as app_module

    (home_dir() / "config.yaml").write_text(f'global:\n  model: "{MODEL}"\n')
    monkeypatch.setenv("ARCHIE_SESSION_ID", "test-session")
    monkeypatch.setattr(host_config, "SYNC_INTERVAL", 0.02)
    with (
        patch.object(app_module, "create_llm_client", return_value=MagicMock()),
        TestClient(app_module.app),
    ):
        (home_dir() / "config.yaml").write_text(
            f'global:\n  model: "{MODEL}"\nplugins:\n  jira: {{enabled: false}}\n'
        )
        deadline = time.monotonic() + 3
        while host_config.current().config().plugins.get("jira") is None:
            assert time.monotonic() < deadline, "the agent never synced"
            time.sleep(0.02)
        assert host_config.current().config().plugins["jira"].enabled is False


# ---------------------------------------------------------------------------
# Agent startup
# ---------------------------------------------------------------------------


async def test_lifespan_exits_when_the_orchestrator_cant_be_asked(monkeypatch, caplog):
    from archie_agent import app as app_module

    monkeypatch.setenv("ARCHIE_SESSION_ID", "test-session")
    monkeypatch.setattr(
        host_config, "source_from_env", lambda: ScriptedSource(HostConfigError("no route"))
    )
    with (
        caplog.at_level(logging.ERROR, logger="archie_agent.app"),
        pytest.raises(SystemExit) as exc,
    ):
        async with app_module.lifespan(app_module.app):
            pass
    assert exc.value.code == 1
    assert "Cannot get the config from the orchestrator: no route" in caplog.text


async def test_kiro_lifespan_records_why_it_has_no_config(monkeypatch):
    from archie_agent import app as app_module

    monkeypatch.setenv("ARCHIE_BACKEND", "kiro")
    monkeypatch.setenv("ARCHIE_SESSION_ID", "kirotest-02")
    (home_dir() / "config.yaml").write_text("plugins: [unclosed\n")
    with pytest.raises(SystemExit) as exc:
        async with app_module.lifespan(app_module.app):
            pass
    assert exc.value.code == 1
    reason = (home_dir() / "logs" / "kiro" / "kirotest-02.startup-error").read_text()
    assert "Cannot get the config from the orchestrator" in reason
    assert "Invalid YAML" in reason


async def test_kiro_lifespan_records_a_local_api_failure(monkeypatch):
    from contextlib import asynccontextmanager

    from archie_agent import app as app_module

    @asynccontextmanager
    async def broken():
        raise OSError("AF_UNIX path too long")
        yield

    monkeypatch.setenv("ARCHIE_BACKEND", "kiro")
    monkeypatch.setenv("ARCHIE_SESSION_ID", "kirotest-03")
    monkeypatch.setattr(app_module.local_api, "serve_local", broken)
    with pytest.raises(SystemExit) as exc:
        async with app_module.lifespan(app_module.app):
            pass
    assert exc.value.code == 1
    reason = (home_dir() / "logs" / "kiro" / "kirotest-03.startup-error").read_text()
    assert "Cannot start the agent's local API: AF_UNIX path too long" in reason


def test_agent_runs_with_the_config_the_orchestrator_serves(monkeypatch):
    from archie_agent import app as app_module

    (home_dir() / "config.yaml").write_text(f'global:\n  model: "{MODEL}"\n  region: "eu-west-1"\n')
    monkeypatch.setenv("ARCHIE_SESSION_ID", "test-session")
    with serve(orchestrator_app) as tracker:
        _live_token(tracker)
        monkeypatch.setattr(host_config, "source_from_env", _http_source)
        with (
            patch.object(app_module, "create_llm_client", return_value=MagicMock()),
            TestClient(app_module.app) as agent,
        ):
            status = agent.get("/status").json()
    assert status["model"] == MODEL
