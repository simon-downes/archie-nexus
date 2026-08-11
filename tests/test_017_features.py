"""Tests for plan 017 features: cost accumulation, /shell endpoint."""

import json

import msgspec
import pytest
from archie_shared.events import (
    IterationStart,
    ModelSwitched,
    SessionInfo,
    Usage,
    deserialize_event,
)
from archie_shared.session.log import MessageEntry
from starlette.testclient import TestClient

# --- Wire event shape tests ---


class TestIterationStartWireEvent:
    """Wire IterationStart event signals a new tool-loop iteration (Bug 2)."""

    def test_to_json(self):
        event = IterationStart(turn_index=3, index=2)
        data = event.to_json()
        assert data["type"] == "iteration_start"
        assert data["turn_index"] == 3
        assert data["data"]["index"] == 2

    def test_from_json(self):
        event = IterationStart.from_json(turn_index=3, data={"index": 2})
        assert event.turn_index == 3
        assert event.index == 2

    def test_serialize_round_trip(self):
        raw = json.dumps(IterationStart(turn_index=1, index=0).to_json())
        result = deserialize_event(raw)
        assert isinstance(result, IterationStart)
        assert result.turn_index == 1
        assert result.index == 0


class TestUsageWireEvent:
    """Wire Usage event carries per-request values (no cost field)."""

    def test_to_json_no_cost(self):
        event = Usage(
            turn_index=1,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            context_pct=42.5,
        )
        data = event.to_json()
        assert data["type"] == "usage"
        assert data["data"]["input_tokens"] == 100
        assert data["data"]["output_tokens"] == 50
        assert data["data"]["cache_read_tokens"] == 10
        assert data["data"]["cache_write_tokens"] == 5
        assert data["data"]["context_pct"] == 42.5
        assert "cost" not in data["data"]

    def test_from_json(self):
        event = Usage.from_json(
            turn_index=1,
            data={
                "input_tokens": 200,
                "output_tokens": 30,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "context_pct": 10.0,
            },
        )
        assert event.input_tokens == 200
        assert event.context_pct == 10.0


class TestSessionInfoCostRates:
    """SessionInfo no longer carries client-side cost rates (029 M7).

    Token/cost accounting is delivered via SessionSnapshot.accounting and
    canonical llm_request ledger events, not derived client-side from rates.
    """

    def test_to_json_has_no_cost_block(self):
        info = SessionInfo(
            protocol_version=1,
            model="Claude Sonnet 4",
            session_id="test-abc",
        )
        assert "cost" not in info.to_json()["data"]

    def test_from_json_ignores_legacy_cost_block(self):
        info = SessionInfo.from_json(
            {
                "protocol_version": 1,
                "model": "Test",
                "session_id": "x",
                "cost": {"input": 1.0, "output": 2.0},
            }
        )
        assert info.model == "Test"
        assert not hasattr(info, "cost_per_m_input")


class TestModelSwitchedCostRates:
    """ModelSwitched no longer carries client-side cost rates (029 M7)."""

    def test_to_json_has_no_cost_block(self):
        event = ModelSwitched(
            model_key="haiku",
            model_name="Haiku",
            supports_cache=True,
        )
        assert "cost" not in event.to_json()["data"]

    def test_from_json_ignores_legacy_cost_block(self):
        event = ModelSwitched.from_json(
            {
                "model_key": "haiku",
                "model_name": "Haiku",
                "supports_cache": True,
                "cost": {"input": 1.0, "output": 5.0},
            }
        )
        assert event.model_key == "haiku"
        assert not hasattr(event, "cost_per_m_input")


# --- /shell endpoint tests ---


class TestShellEndpoint:
    """Test the /shell POST endpoint logs to session JSONL."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        """Create a test client with a running agent."""
        from archie_agent.app import app, lifespan  # noqa: F401

        # We need to set up the agent before the app can handle requests
        # Use the same pattern as test_ws_integration
        monkeypatch.setenv("ARCHIE_SESSION_ID", "test-shell-001")
        monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / "config.yaml").write_text(
            'global:\n  model: "bedrock-claude-sonnet-4-6"\n  region: "eu-west-1"\n'
        )
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def _reset_agent(self):
        """Ensure _agent is always reset after each test, even on failure."""
        from archie_agent import app as app_module

        yield
        app_module._agent = None

    def test_shell_log_creates_entry(self, client, tmp_path):
        """POST /shell writes a MessageEntry with role=shell to log."""
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

        # Verify log entry
        log_content = MockAgent.log_path.read_text()
        entry = msgspec.json.decode(log_content.strip(), type=MessageEntry)
        assert entry.role == "shell"
        content = json.loads(entry.content)
        assert content["command"] == "ls -l"
        assert content["exit_code"] == 0
        assert content["output"] == "total 0\n"

    def test_shell_log_no_agent(self, client):
        """POST /shell returns 503 when no agent is running."""
        response = client.post(
            "/shell",
            json={"command": "pwd", "exit_code": 0, "output": "/workspace\n"},
        )
        assert response.status_code == 503
