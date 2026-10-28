"""TUI model palette and status name for Kiro and native sessions (077 D25)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from archie_cli.tui import models_provider
from archie_cli.tui.models_provider import ModelProvider, model_entries
from archie_shared.events import SessionStatus
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry, Rate

CATALOG = {
    "model-sonnet": ModelEntry(
        name="Claude Sonnet 4.6",
        context=200_000,
        provider=BedrockProvider(model_id="ep"),
        cost=CostConfig(rates=[Rate(input=3.0, output=15.0)]),
    )
}


def test_kiro_session_lists_allowlisted_kiro_models_only(monkeypatch):
    monkeypatch.setattr(models_provider, "_get_catalog", lambda: CATALOG)
    monkeypatch.setattr(
        models_provider, "_kiro_models", lambda: ("gpt-5.6-luna", "claude-opus-5.5")
    )
    entries = model_entries("kiro/gpt-5.6-luna")
    assert [key for key, _, _ in entries] == ["kiro/gpt-5.6-luna", "kiro/claude-opus-5.5"]
    assert entries[0][1] == "Change Model → gpt-5.6-luna (Kiro)"


def test_native_session_lists_catalog(monkeypatch):
    monkeypatch.setattr(models_provider, "_get_catalog", lambda: CATALOG)
    monkeypatch.setattr(models_provider, "_kiro_models", lambda: ("gpt-5.6-luna",))
    entries = model_entries("model-sonnet")
    assert entries == [
        ("model-sonnet", "Change Model → Claude Sonnet 4.6", "$3.00/$15.00 per M tokens")
    ]


async def test_provider_discover_uses_session_key(monkeypatch):
    monkeypatch.setattr(models_provider, "_get_catalog", lambda: CATALOG)
    monkeypatch.setattr(models_provider, "_kiro_models", lambda: ("gpt-5.6-luna",))
    provider = ModelProvider.__new__(ModelProvider)
    app = MagicMock()
    app._session_model_key = "kiro/gpt-5.6-luna"
    provider._Provider__screen = MagicMock(app=app)
    hits = [h async for h in provider.discover()]
    assert [str(h.display) for h in hits] == ["Change Model → gpt-5.6-luna (Kiro)", "Quit"]


def _status_name(model_key: str) -> str:
    from archie_cli.tui.app import ArchieApp

    app = ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/p-01/stream",
        api_url="http://127.0.0.1:7600/sessions/p-01",
        container_name="archie-p-01",
    )
    status = MagicMock()
    with patch.object(app, "query_one", return_value=status):
        app._apply_event(SessionStatus(id="s1", model_key=model_key, git_branch="main"))
    return status.model_name


def test_status_bar_shows_kiro_model():
    assert _status_name("kiro/claude-opus-5.5") == "claude-opus-5.5 (Kiro)"


def test_status_bar_shows_native_model_name():
    from archie_shared.models import load_models

    key, model = next(iter(load_models().items()))
    assert _status_name(key) == model.name
