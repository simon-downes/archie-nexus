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
    """SessionInfo includes cost rates for client-side computation."""

    def test_to_json_includes_cost(self):
        info = SessionInfo(
            protocol_version=1,
            model="Claude Sonnet 4",
            session_id="test-abc",
            cost_per_m_input=3.0,
            cost_per_m_output=15.0,
            cost_per_m_cache_read=0.3,
            cost_per_m_cache_write=3.75,
        )
        data = info.to_json()
        cost = data["data"]["cost"]
        assert cost["input"] == 3.0
        assert cost["output"] == 15.0
        assert cost["cache_read"] == 0.3
        assert cost["cache_write"] == 3.75

    def test_from_json_with_cost(self):
        info = SessionInfo.from_json(
            {
                "protocol_version": 1,
                "model": "Test",
                "session_id": "x",
                "cost": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 0.5},
            }
        )
        assert info.cost_per_m_input == 1.0
        assert info.cost_per_m_output == 2.0

    def test_from_json_without_cost_defaults_zero(self):
        info = SessionInfo.from_json(
            {"protocol_version": 1, "model": "Test", "session_id": "x"}
        )
        assert info.cost_per_m_input == 0.0
        assert info.cost_per_m_output == 0.0


class TestModelSwitchedCostRates:
    """ModelSwitched includes cost rates for mid-session updates."""

    def test_to_json_includes_cost(self):
        event = ModelSwitched(
            model_key="haiku",
            model_name="Haiku",
            supports_cache=True,
            cost_per_m_input=1.0,
            cost_per_m_output=5.0,
        )
        cost = event.to_json()["data"]["cost"]
        assert cost["input"] == 1.0
        assert cost["output"] == 5.0

    def test_from_json_with_cost(self):
        event = ModelSwitched.from_json(
            {
                "model_key": "haiku",
                "model_name": "Haiku",
                "supports_cache": True,
                "cost": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.0},
            }
        )
        assert event.cost_per_m_input == 1.0
        assert event.cost_per_m_cache_write == 1.0


# --- Cost accumulation math ---


class TestCostAccumulation:
    """Cost accumulation using the real calculate_cost function from shared."""

    def test_single_usage_delta(self):
        """Cost delta matches calculate_cost applied to per-request values."""
        from archie_shared.models import CostConfig, calculate_cost

        config = CostConfig(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
        cost = calculate_cost(config, input_tokens=1000, output_tokens=500,
                              cache_read_tokens=200, cache_write_tokens=100)
        # 1000*3/1M + 500*15/1M + 200*0.3/1M + 100*3.75/1M
        # = 0.003 + 0.0075 + 0.00006 + 0.000375 = 0.010935
        assert abs(cost - 0.010935) < 1e-10

    def test_model_switch_uses_new_rates(self):
        """After model switch, new Usage events use the new config's rates."""
        from archie_shared.models import CostConfig, calculate_cost

        # Simulate TUI accumulation across a model switch
        cumulative_cost = 0.0

        # First model (expensive)
        config1 = CostConfig(input=3.0, output=15.0)
        cumulative_cost += calculate_cost(config1, input_tokens=1000, output_tokens=500)
        # = 0.003 + 0.0075 = 0.0105

        # Switch to cheap model
        config2 = CostConfig(input=0.8, output=4.0)
        cumulative_cost += calculate_cost(config2, input_tokens=1000, output_tokens=500)
        # += 0.0008 + 0.002 = 0.0028
        # total = 0.0133

        assert abs(cumulative_cost - 0.0133) < 1e-10

    def test_zero_rates_zero_cost(self):
        """Zero cost config (e.g. ollama) produces zero cost."""
        from archie_shared.models import CostConfig, calculate_cost

        config = CostConfig()  # all zeros
        cost = calculate_cost(config, input_tokens=10000, output_tokens=5000)
        assert cost == 0.0


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
