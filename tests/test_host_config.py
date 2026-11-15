"""The agent's host config: environment, HTTP source, cache and startup (plan 090)."""

import logging
import os
from unittest.mock import MagicMock, patch

import httpx
import msgspec
import pytest
from archie_agent import host_config
from archie_agent.host_config import HostConfig, HostConfigError, HttpSource
from archie_orchestrator.app import app as orchestrator_app
from archie_shared.agent_sync import SyncItem, SyncResponse, token_hash, version_of
from archie_shared.config import ConfigError, home_dir
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
