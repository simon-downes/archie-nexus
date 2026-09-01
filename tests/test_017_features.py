"""Tests for canonical live events and the /shell endpoint."""

import pytest
from archie_agent.session_bus import SessionEventBus
from archie_shared.canonical_events import (
    ErrorNotice,
    Handshake,
    ModelSwitch,
    ShellCommand,
    StatusUpdated,
    decode_event,
    encode_event,
)
from starlette.testclient import TestClient
from ulid import ULID


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

    def test_shell_log_creates_canonical_event(self, client, tmp_path):
        from archie_agent import app as app_module

        log_path = tmp_path / "session.jsonl"

        class MockAgent:
            pass

        MockAgent.log_path = log_path
        MockAgent.event_bus = SessionEventBus(log_path)

        app_module._agent = MockAgent()
        response = client.post(
            "/shell",
            json={"command": "ls -l", "exit_code": 0, "output": "total 0\n"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        events = [
            decode_event(line, persisted=True) for line in log_path.read_text().splitlines() if line
        ]
        assert len(events) == 1
        assert events[0] == ShellCommand(
            id=events[0].id,
            command="ls -l",
            exit_code=0,
            output="total 0\n",
        )

        replay = client.get("/events")
        assert replay.status_code == 200
        assert decode_event(replay.text.strip(), persisted=True) == events[0]

    def test_shell_log_preserves_nonzero_exit_with_empty_output(self, client, tmp_path):
        from archie_agent import app as app_module

        log_path = tmp_path / "session.jsonl"

        class MockAgent:
            pass

        MockAgent.log_path = log_path
        MockAgent.event_bus = SessionEventBus(log_path)
        app_module._agent = MockAgent()

        response = client.post(
            "/shell",
            json={"command": "false", "exit_code": 1, "output": ""},
        )

        assert response.status_code == 200
        event = decode_event(log_path.read_text().strip(), persisted=True)
        assert event == ShellCommand(
            id=event.id,
            command="false",
            exit_code=1,
            output="",
        )
        replay = client.get("/events")
        assert replay.status_code == 200
        assert decode_event(replay.text.strip(), persisted=True) == event

    def test_shell_log_preserves_client_ulid_and_idempotent_duplicate(self, client, tmp_path):
        from archie_agent import app as app_module

        log_path = tmp_path / "session.jsonl"

        class MockAgent:
            pass

        MockAgent.log_path = log_path
        MockAgent.event_bus = SessionEventBus(log_path)
        app_module._agent = MockAgent()

        event_id = str(ULID())
        payload = {
            "command": "printf ok",
            "exit_code": 0,
            "output": "ok",
            "event_id": event_id,
        }
        first = client.post("/shell", json=payload)
        duplicate = client.post("/shell", json=payload)
        conflict = client.post("/shell", json={**payload, "output": "different"})

        assert first.status_code == 200
        assert duplicate.status_code == 200
        assert conflict.status_code == 409
        event = decode_event(log_path.read_text().strip(), persisted=True)
        assert event.id == event_id
        assert event.output == "ok"

    @pytest.mark.parametrize("event_id", ["x", "0" * 25, "!" * 26])
    def test_shell_log_rejects_malformed_event_ids(self, client, tmp_path, event_id):
        from archie_agent import app as app_module

        log_path = tmp_path / "session.jsonl"

        class MockAgent:
            pass

        MockAgent.log_path = log_path
        MockAgent.event_bus = SessionEventBus(log_path)
        app_module._agent = MockAgent()

        response = client.post(
            "/shell",
            json={
                "command": "echo ok",
                "exit_code": 0,
                "output": "ok",
                "event_id": event_id,
            },
        )

        assert response.status_code == 400
        assert not log_path.exists()

    @pytest.mark.parametrize(
        "payload",
        [
            {"command": 123, "exit_code": 0, "output": "ok"},
            {"command": "echo ok", "exit_code": True, "output": "ok"},
            {"command": "echo ok", "exit_code": 0, "output": ["ok"]},
        ],
    )
    def test_shell_log_rejects_invalid_payload_types(self, client, tmp_path, payload):
        from archie_agent import app as app_module

        log_path = tmp_path / "session.jsonl"

        class MockAgent:
            pass

        MockAgent.log_path = log_path
        MockAgent.event_bus = SessionEventBus(log_path)
        app_module._agent = MockAgent()

        response = client.post("/shell", json=payload)

        assert response.status_code == 400
        assert not log_path.exists()

    def test_shell_log_no_agent(self, client):
        response = client.post(
            "/shell",
            json={"command": "pwd", "exit_code": 0, "output": "/workspace\n"},
        )
        assert response.status_code == 503
