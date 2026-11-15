"""Agent config sync on the orchestrator: replies, token auth and storage (plan 090)."""

import dataclasses
import sqlite3

import msgspec
import pytest
from archie_orchestrator.agent_api import answer_sync
from archie_orchestrator.app import app
from archie_orchestrator.docker import DockerError
from archie_orchestrator.store import SessionStore, db_path
from archie_shared.agent_sync import SyncItem, SyncRequest, SyncResponse, token_hash, version_of
from archie_shared.config import ConfigError, home_dir
from archie_shared.schemas import NexusConfig
from starlette.testclient import TestClient

from tests.session_fakes import FakeRuntime, make_tracker, record, serve

TOKEN = "agent-token-0123456789"
LIVE = "proj-01ktoken0001"
DEFAULTS = msgspec.to_builtins(NexusConfig())


def _answer(request, *, config=None, config_error=None, store=None):
    def load_config():
        if config_error is not None:
            raise ConfigError(config_error)
        return config or NexusConfig()

    def load_store():
        if isinstance(store, Exception):
            raise store
        return store or {}

    return answer_sync(request, load_config=load_config, load_store=load_store)


# ---------------------------------------------------------------------------
# answer_sync
# ---------------------------------------------------------------------------


def test_config_not_held_is_sent_with_its_version():
    response = _answer(SyncRequest())
    assert response.config == SyncItem(version=version_of(DEFAULTS), value=DEFAULTS)
    assert (response.config_error, response.credentials, response.credential_errors) == (
        None,
        {},
        {},
    )


def test_unchanged_versions_send_nothing():
    entry = {"personal_token": "p"}
    request = SyncRequest(config=version_of(DEFAULTS), credentials={"github": version_of(entry)})
    assert _answer(request, store={"github": entry}) == SyncResponse()


def test_changed_and_absent_credentials_are_sent():
    entry = {"personal_token": "new"}
    request = SyncRequest(
        config=version_of(DEFAULTS),
        credentials={"github": version_of({"personal_token": "old"}), "notion": None},
    )
    response = _answer(request, store={"github": entry})
    assert response.config is None
    assert response.credentials == {
        "github": SyncItem(version=version_of(entry), value=entry),
        "notion": SyncItem(version=version_of(None), value=None),
    }


def test_version_is_16_hex_and_ignores_key_order():
    assert version_of({"a": 1, "b": 2}) == version_of({"b": 2, "a": 1})
    assert len(version_of(None)) == 16
    assert version_of({"a": 1}) != version_of({"a": 2})


def test_invalid_config_is_reported_and_not_sent():
    response = _answer(SyncRequest(), config_error="Validation error in config.yaml: bad key")
    assert response.config is None
    assert response.config_error == "Validation error in config.yaml: bad key"


def test_unknown_provider_and_non_mapping_entry_are_errors():
    request = SyncRequest(credentials={"nosuch": None, "github": None})
    response = _answer(request, store={"github": "oops"})
    assert response.credentials == {}
    assert response.credential_errors == {
        "nosuch": "unknown provider 'nosuch'",
        "github": "the stored 'github' entry isn't a mapping",
    }


def test_unreadable_store_names_only_the_exception_type():
    request = SyncRequest(credentials={"github": None})
    response = _answer(request, store=ValueError("Invalid YAML: personal_token: s3cret"))
    assert response.credential_errors == {
        "github": "the credential store can't be read (ValueError)"
    }
    assert b"s3cret" not in msgspec.json.encode(response)


def test_store_is_not_read_without_credentials():
    response = _answer(SyncRequest(config=version_of(DEFAULTS)), store=RuntimeError("unread"))
    assert response == SyncResponse()


# ---------------------------------------------------------------------------
# POST /api/agent/sync
# ---------------------------------------------------------------------------


def _with_token(tracker, session_id: str, token: str, *, live: bool = True) -> None:
    item = record(session_id) if live else record(session_id, status="stopped", ended_at="x")
    tracker.store.insert(item, agent_token_hash=token_hash(token))
    if live:
        tracker._records[session_id] = item


def _post(headers: dict, body: bytes = b"{}"):
    return TestClient(app).post("/api/agent/sync", content=body, headers=headers)


def test_sync_with_a_live_sessions_token_returns_the_host_config():
    (home_dir() / "config.yaml").write_text("global:\n  model: bedrock-claude-sonnet-4-6\n")
    with serve(app) as tracker:
        _with_token(tracker, LIVE, TOKEN)
        response = _post({"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = msgspec.json.decode(response.content, type=SyncResponse)
    assert body.config is not None
    assert body.config.value["global"]["model"] == "bedrock-claude-sonnet-4-6"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer someone-else"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
    ],
    ids=["no-header", "empty", "unknown", "no-scheme", "basic"],
)
def test_sync_without_a_valid_token_is_401(headers):
    with serve(app) as tracker:
        _with_token(tracker, LIVE, TOKEN)
        response = _post(headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_sync_with_an_ended_sessions_token_is_401():
    with serve(app) as tracker:
        _with_token(tracker, LIVE, TOKEN, live=False)
        response = _post({"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 401


def test_sync_rejects_a_malformed_body():
    with serve(app) as tracker:
        _with_token(tracker, LIVE, TOKEN)
        response = _post({"Authorization": f"Bearer {TOKEN}"}, b'{"config": 5}')
    assert response.status_code == 400
    assert response.json()["error"].startswith("invalid sync request")


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


def test_token_hash_is_stored_looked_up_and_kept_by_record_writes(tmp_path):
    store = SessionStore(db_path(tmp_path))
    store.migrate()
    store.insert(record("a-01k0000001"), agent_token_hash="h1")
    store.insert(record("b-01k0000002"))
    store.insert(record("c-01k0000003"))  # a second row without a token
    store.save(record("a-01k0000001", status="working"))
    assert store.session_for_token_hash("h1") == "a-01k0000001"
    assert store.session_for_token_hash("h2") is None
    with pytest.raises(sqlite3.IntegrityError):
        store.insert(record("d-01k0000004"), agent_token_hash="h1")


class _TokenRuntime(FakeRuntime):
    def prepare(self, workspace, config, backend):
        prepared = super().prepare(workspace, config, backend)
        return dataclasses.replace(prepared, agent_token=f"token-{prepared.session_id}")


async def test_create_stores_the_hash_of_the_prepared_token():
    tracker = make_tracker(runtime=_TokenRuntime(run_error=DockerError("docker run", "x", 1)))
    created = await tracker.create("proj", backend="native", name=None, config=NexusConfig())
    expected = token_hash(f"token-{created.session_id}")
    assert tracker.store.session_for_token_hash(expected) == created.session_id
    await tracker.close()


async def test_create_without_a_token_stores_none():
    tracker = make_tracker(runtime=FakeRuntime(run_error=DockerError("docker run", "x", 1)))
    await tracker.create("proj", backend="native", name=None, config=NexusConfig())
    await tracker.create("proj", backend="native", name=None, config=NexusConfig())
    assert tracker.store.session_for_token_hash(token_hash("")) is None
    await tracker.close()
