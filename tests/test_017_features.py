"""Tests for canonical live events and the /shell endpoint."""

import json

import msgspec
import pytest
from archie_shared.canonical_events import (
    ErrorNotice,
    Handshake,
    ModelSwitch,
    StatusUpdated,
    decode_event,
    encode_event,
)
from archie_shared.session.log import MessageEntry
from starlette.testclient import TestClient


def test_live_event_round_trip():
    event = ErrorNotice(
        id="01J00000000000000000000001",
        kind="turn_active",
        message="Turn already active",
    )
    restored = decode_event(encode_event(event))
    assert restored == event


def test_handshake_contains_only_connection_metadata():
    event = Handshake(
        id="01J00000000000000000000001",
        protocol_version=2,
        session_id="test-abc",
        model_key="model-key",
    )
    raw = encode_event(event)
    assert "total_cost" not in raw
    assert "latest_event_id" not in raw
    assert decode_event(raw) == event


def test_status_and_model_switch_are_canonical():
    status = StatusUpdated(id="01J00000000000000000000001", git_branch="main")
    switch = ModelSwitch(
        id="01J00000000000000000000002",
        model_key="haiku",
        sent_at="2025-01-01T00:00:00+00:00",
    )
    assert decode_event(encode_event(status)) == status
    assert decode_event(encode_event(switch), persisted=True) == switch
    assert "model_name" not in encode_event(switch)
    assert "supports_cache" not in encode_event(switch)


class TestShellEndpoint:
    """Test the /shell POST endpoint logs to session JSONL."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from archie_agent.app import app, lifespan  # noqa: F401

        monkeypatch.setenv("ARCHIE_SESSION_ID", "test-shell-001")
        monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / "config.yaml").write_text(
            'global:\n  model: "bedrock-claude-sonnet-4-6"\n  region: "eu-west-1"\n'
        )
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def _reset_agent(self):
        from archie_agent import app as app_module

        yield
        app_module._agent = None

    def test_shell_log_creates_entry(self, client, tmp_path):
        from archie_agent import app as app_module

        class MockAgent:
            log_path = tmp_path / "session.jsonl"

        app_module._agent = MockAgent()
        response = client.post(
            "/shell",
            json={"command": "ls -l", "exit_code": 0, "output": "total 0\n"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        entry = msgspec.json.decode(MockAgent.log_path.read_text().strip(), type=MessageEntry)
        assert entry.role == "shell"
        content = json.loads(entry.content)
        assert content == {"command": "ls -l", "exit_code": 0, "output": "total 0\n"}

    def test_shell_log_no_agent(self, client):
        response = client.post(
            "/shell",
            json={"command": "pwd", "exit_code": 0, "output": "/workspace\n"},
        )
        assert response.status_code == 503
