"""Tests for SwitchModelCommand and ModelSwitched wire protocol types."""

import pytest
from archie_shared.events import (
    ModelSwitched,
    SwitchModelCommand,
    TurnError,
    deserialize_command,
    deserialize_event,
    serialize_command,
    serialize_event,
)
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry


class TestSwitchModelCommand:
    """Tests for SwitchModelCommand serialization."""

    def test_to_json(self):
        cmd = SwitchModelCommand(model_key="bedrock-claude-haiku-4-5")
        data = cmd.to_json()
        assert data == {
            "type": "switch_model",
            "data": {"model_key": "bedrock-claude-haiku-4-5"},
        }

    def test_from_json(self):
        cmd = SwitchModelCommand.from_json({"model_key": "bedrock-claude-haiku-4-5"})
        assert cmd.model_key == "bedrock-claude-haiku-4-5"

    def test_serialize_round_trip(self):
        cmd = SwitchModelCommand(model_key="bedrock-claude-sonnet-4-6")
        raw = serialize_command(cmd)
        result = deserialize_command(raw)
        assert isinstance(result, SwitchModelCommand)
        assert result.model_key == "bedrock-claude-sonnet-4-6"

    def test_deserialize_from_json_string(self):
        raw = '{"type": "switch_model", "data": {"model_key": "my-model"}}'
        result = deserialize_command(raw)
        assert isinstance(result, SwitchModelCommand)
        assert result.model_key == "my-model"


class TestModelSwitched:
    """Tests for ModelSwitched event serialization."""

    def test_to_json(self):
        event = ModelSwitched(
            model_key="bedrock-claude-haiku-4-5",
            model_name="Claude Haiku 4.5",
            supports_cache=True,
        )
        data = event.to_json()
        assert data == {
            "type": "model_switched",
            "data": {
                "model_key": "bedrock-claude-haiku-4-5",
                "model_name": "Claude Haiku 4.5",
                "supports_cache": True,
            },
        }

    def test_from_json(self):
        event = ModelSwitched.from_json(
            {
                "model_key": "bedrock-claude-haiku-4-5",
                "model_name": "Claude Haiku 4.5",
                "supports_cache": True,
            }
        )
        assert event.model_key == "bedrock-claude-haiku-4-5"
        assert event.model_name == "Claude Haiku 4.5"
        assert event.supports_cache is True

    def test_from_json_defaults_supports_cache(self):
        """Missing supports_cache defaults to False (backward compat)."""
        event = ModelSwitched.from_json({"model_key": "k", "model_name": "n"})
        assert event.supports_cache is False

    def test_serialize_round_trip(self):
        event = ModelSwitched(
            model_key="bedrock-claude-sonnet-4-6",
            model_name="Claude Sonnet 4.6",
            supports_cache=True,
        )
        raw = serialize_event(event)
        result = deserialize_event(raw)
        assert isinstance(result, ModelSwitched)
        assert result.model_key == "bedrock-claude-sonnet-4-6"
        assert result.model_name == "Claude Sonnet 4.6"
        assert result.supports_cache is True

    def test_no_turn_index_in_wire_format(self):
        """ModelSwitched is a session-level event — no turn_index in JSON."""
        event = ModelSwitched(model_key="k", model_name="n")
        data = event.to_json()
        assert "turn_index" not in data


# ---------------------------------------------------------------------------
# Agent-side model switch handler tests (via WS integration)
# ---------------------------------------------------------------------------


class TestModelSwitchHandler:
    """Tests for _handle_model_switch in app.py via the Starlette test client."""

    @pytest.fixture
    def _setup_app(self, tmp_path, monkeypatch):
        """Set up the app with a test model catalog."""
        import archie_agent.app as app_module

        test_model_a = ModelEntry(
            name="Test Model A",
            context=200_000,
            provider=BedrockProvider(model_id="test-a-endpoint", region="us-east-1"),
            cost=CostConfig(input=3.0, output=15.0),
            max_output_tokens=4096,
            can_cache=True,
        )
        test_model_b = ModelEntry(
            name="Test Model B",
            context=100_000,
            provider=BedrockProvider(model_id="test-b-endpoint"),
            cost=CostConfig(input=1.0, output=5.0),
            max_output_tokens=8192,
            can_cache=False,
        )

        from archie_agent.harness import AgentHarness
        from archie_agent.llm.fake import FakeLLMClient
        from archie_agent.session import Session

        session = Session(model_id="model-a", model=test_model_a, session_id="test-sess")
        llm = FakeLLMClient(responses=[])

        harness = AgentHarness(
            session=session,
            llm_client=llm,
            model_name="Test Model A",
            log_dir=tmp_path,
        )

        catalog = {"model-a": test_model_a, "model-b": test_model_b}

        # Mock create_llm_client so it doesn't try to connect to AWS/Ollama
        class FakeLLMClient:
            def __init__(self, model_id="", region="", **kwargs):
                self.model_id = model_id

        def fake_create_llm_client(model, default_region):
            return FakeLLMClient(model_id=model.provider.model_id)

        monkeypatch.setattr(app_module, "create_llm_client", fake_create_llm_client)

        # Monkeypatch module-level state
        monkeypatch.setattr(app_module, "_agent", harness)
        monkeypatch.setattr(app_module, "_catalog", catalog)
        monkeypatch.setattr(
            app_module,
            "_config",
            type("C", (), {"global_": type("G", (), {"region": "us-west-2"})()})(),
        )

        return harness, catalog

    @pytest.mark.asyncio
    async def test_switch_model_success(self, tmp_path, monkeypatch, _setup_app):
        """Successful model switch updates session and LLM."""
        harness, catalog = _setup_app

        from archie_agent.app import _handle_model_switch

        # Create a fake websocket that captures sent messages
        sent: list[str] = []

        class FakeWS:
            async def send_text(self, data):
                sent.append(data)

        cmd = SwitchModelCommand(model_key="model-b")
        await _handle_model_switch(cmd, FakeWS())

        # Verify session updated
        assert harness.session.model_id == "model-b"
        assert harness.session.model.name == "Test Model B"

        # Verify LLM client rebuilt
        assert harness._llm.model_id == "test-b-endpoint"

        # Verify model name updated for prompt rebuild
        assert harness._model_name == "Test Model B"

        # Verify broadcast sent (goes to harness.clients, not websocket directly)
        # The direct websocket doesn't get it — it's broadcast to all _agent.clients
        # So sent should be empty (broadcast goes via _agent._broadcast)
        # Let's check by adding the fake WS to clients
        assert len(sent) == 0  # broadcast goes to harness.clients, not the requesting WS

    @pytest.mark.asyncio
    async def test_switch_model_broadcasts_to_clients(self, tmp_path, monkeypatch, _setup_app):
        """ModelSwitched event is broadcast to all connected clients."""
        harness, _ = _setup_app

        sent: list[str] = []

        class FakeWS:
            async def send_text(self, data):
                sent.append(data)

        ws = FakeWS()
        harness.clients.add(ws)

        from archie_agent.app import _handle_model_switch

        cmd = SwitchModelCommand(model_key="model-b")
        await _handle_model_switch(cmd, FakeWS())

        # Client should have received the ModelSwitched event
        assert len(sent) == 1
        event = deserialize_event(sent[0])
        assert isinstance(event, ModelSwitched)
        assert event.model_key == "model-b"
        assert event.model_name == "Test Model B"
        assert event.supports_cache is False  # model-b has can_cache=False

    @pytest.mark.asyncio
    async def test_switch_model_during_active_turn(self, tmp_path, monkeypatch, _setup_app):
        """Switching during active turn returns TurnError."""
        harness, _ = _setup_app
        harness._turn_active = True

        sent: list[str] = []

        class FakeWS:
            async def send_text(self, data):
                sent.append(data)

        from archie_agent.app import _handle_model_switch

        cmd = SwitchModelCommand(model_key="model-b")
        await _handle_model_switch(cmd, FakeWS())

        # Should get a TurnError on the requesting WS
        assert len(sent) == 1
        event = deserialize_event(sent[0])
        assert isinstance(event, TurnError)
        assert "active turn" in event.message.lower()

        # Session should NOT have changed
        assert harness.session.model_id == "model-a"

    @pytest.mark.asyncio
    async def test_switch_model_invalid_key(self, tmp_path, monkeypatch, _setup_app):
        """Unknown model key returns TurnError."""
        harness, _ = _setup_app

        sent: list[str] = []

        class FakeWS:
            async def send_text(self, data):
                sent.append(data)

        from archie_agent.app import _handle_model_switch

        cmd = SwitchModelCommand(model_key="nonexistent-model")
        await _handle_model_switch(cmd, FakeWS())

        assert len(sent) == 1
        event = deserialize_event(sent[0])
        assert isinstance(event, TurnError)
        assert "nonexistent-model" in event.message

        # Session unchanged
        assert harness.session.model_id == "model-a"

    @pytest.mark.asyncio
    async def test_switch_model_uses_config_region_fallback(
        self, tmp_path, monkeypatch, _setup_app
    ):
        """Model with no explicit region uses config.global_.region via factory."""
        harness, _ = _setup_app
        import archie_agent.app as app_module
        from archie_agent.app import _handle_model_switch

        # Track what the factory receives
        factory_calls = []

        def tracking_factory(model, default_region):
            factory_calls.append((model.provider.model_id, default_region))

            class FakeClient:
                model_id = model.provider.model_id

            return FakeClient()

        monkeypatch.setattr(app_module, "create_llm_client", tracking_factory)

        class FakeWS:
            async def send_text(self, data):
                pass

        cmd = SwitchModelCommand(model_key="model-b")
        await _handle_model_switch(cmd, FakeWS())

        # model-b has region=None, so factory should receive config's us-west-2
        assert factory_calls[-1] == ("test-b-endpoint", "us-west-2")


# ---------------------------------------------------------------------------
# TUI ModelProvider tests
# ---------------------------------------------------------------------------


class TestModelProvider:
    """Tests for the TUI ModelProvider command palette."""

    @pytest.mark.asyncio
    async def test_search_returns_hits(self, monkeypatch):
        """Provider yields hits matching the query."""
        from archie_cli.tui.models_provider import ModelProvider

        # Mock load_models to return a test catalog
        test_catalog = {
            "model-haiku": ModelEntry(
                name="Claude Haiku 4.5",
                context=200_000,
                provider=BedrockProvider(model_id="ep-haiku"),
                cost=CostConfig(input=1.0, output=5.0),
            ),
            "model-sonnet": ModelEntry(
                name="Claude Sonnet 4.6",
                context=200_000,
                provider=BedrockProvider(model_id="ep-sonnet"),
                cost=CostConfig(input=3.0, output=15.0),
            ),
        }
        monkeypatch.setattr("archie_cli.tui.models_provider._get_catalog", lambda: test_catalog)

        # Create a minimal provider with mocked internals
        class FakeMatcher:
            def match(self, text):
                return 80.0 if "haiku" in text.lower() else 0.0

            def highlight(self, text):
                return text

        class FakeApp:
            def switch_model(self, key):
                pass

        provider = ModelProvider.__new__(ModelProvider)
        provider.matcher = lambda _q: FakeMatcher()
        object.__setattr__(provider, "_app", FakeApp())

        hits = [h async for h in provider.search("haiku")]
        assert len(hits) == 1
        assert hits[0].help == "$1.00/$5.00 per M tokens"

    @pytest.mark.asyncio
    async def test_search_no_match_returns_empty(self, monkeypatch):
        """Provider yields nothing when no models match."""
        from archie_cli.tui.models_provider import ModelProvider

        test_catalog = {
            "model-sonnet": ModelEntry(
                name="Claude Sonnet 4.6",
                context=200_000,
                provider=BedrockProvider(model_id="ep"),
                cost=CostConfig(input=3.0, output=15.0),
            ),
        }
        monkeypatch.setattr("archie_cli.tui.models_provider._get_catalog", lambda: test_catalog)

        class FakeMatcher:
            def match(self, text):
                return 0.0

            def highlight(self, text):
                return text

        class FakeApp:
            pass

        provider = ModelProvider.__new__(ModelProvider)
        provider.matcher = lambda _q: FakeMatcher()
        object.__setattr__(provider, "_app", FakeApp())

        hits = [h async for h in provider.search("xyz")]
        assert hits == []
