"""The palette's model choices, prices and providers, and the status bar's model name (077 D25,
084 D23).

Ported from the model palette tests when the palette moved into ``commands.py`` (084 M7).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from archie_cli.tui import commands
from archie_cli.tui.commands import (
    KIRO_PRICE,
    ModelChoice,
    ModelCommands,
    model_choices,
    model_price,
)
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


def _entry(*rates: Rate) -> ModelEntry:
    return ModelEntry(
        name="M",
        context=1000,
        provider=BedrockProvider(model_id="ep"),
        cost=CostConfig(rates=list(rates)),
    )


def test_kiro_session_lists_allowlisted_kiro_models_only(monkeypatch):
    monkeypatch.setattr(commands, "_get_catalog", lambda: CATALOG)
    monkeypatch.setattr(commands, "_kiro_models", lambda: ("gpt-5.6-luna", "claude-opus-5.5"))
    choices = model_choices("kiro/gpt-5.6-luna")
    assert [c.key for c in choices] == ["kiro/gpt-5.6-luna", "kiro/claude-opus-5.5"]
    assert choices[0] == ModelChoice("kiro/gpt-5.6-luna", "gpt-5.6-luna (Kiro)", KIRO_PRICE)


def test_native_session_lists_catalog(monkeypatch):
    monkeypatch.setattr(commands, "_get_catalog", lambda: CATALOG)
    monkeypatch.setattr(commands, "_kiro_models", lambda: ("gpt-5.6-luna",))
    assert model_choices("model-sonnet") == [
        ModelChoice("model-sonnet", "Claude Sonnet 4.6", " $3.00 / $15.00")
    ]


def test_prices_are_aligned_and_free_and_tiered_are_named():
    assert model_price(_entry(Rate(input=1.0, output=5.0))) == " $1.00 /  $5.00"
    assert model_price(_entry(Rate(input=15.0, output=75.0))) == "$15.00 / $75.00"
    assert model_price(_entry(Rate(input=0.0, output=0.0))) == "free"
    tiered = _entry(
        Rate(input=3.0, output=15.0),
        Rate(input=6.0, output=22.5, criteria=("total_input", "min", 200_000)),
    )
    assert model_price(tiered) == " $3.00 / $15.00 tiered"


async def test_provider_discover_uses_session_key(monkeypatch):
    monkeypatch.setattr(commands, "_get_catalog", lambda: CATALOG)
    monkeypatch.setattr(commands, "_kiro_models", lambda: ("gpt-5.6-luna",))
    switched = []

    class FakeApp:
        session_model_key = "kiro/gpt-5.6-luna"

        def switch_model(self, key):
            switched.append(key)

    provider = ModelCommands.__new__(ModelCommands)
    provider._Provider__screen = MagicMock(app=FakeApp())
    hits = [h async for h in provider.discover()]
    assert [h.text for h in hits] == ["gpt-5.6-luna (Kiro)"]
    assert hits[0].prompt.plain == f"✓ gpt-5.6-luna (Kiro)   {KIRO_PRICE}"
    hits[0].command()
    assert switched == ["kiro/gpt-5.6-luna"]


async def _status_name(model_key: str) -> str:
    from archie_cli.tui.status import StatusBar

    from tests.tui_fakes import apply_events, attached, make_app

    app = make_app()
    async with app.run_test(size=(150, 30)) as pilot:
        await attached(app, pilot)
        await apply_events(
            app, pilot, SessionStatus(id="s1", model_key=model_key, git_branch="main")
        )
        return app.pane.query_one(StatusBar).model_name


async def test_status_bar_shows_kiro_model():
    assert await _status_name("kiro/claude-opus-5.5") == "claude-opus-5.5 (Kiro)"


async def test_status_bar_shows_native_model_name():
    from archie_shared.models import load_models

    key, model = next(iter(load_models().items()))
    assert await _status_name(key) == model.name


async def test_app_points_the_picker_at_the_on_screen_session():
    from tests.tui_fakes import FakeOrchestrator, attached, make_app, make_record

    client = FakeOrchestrator(
        sessions=[
            make_record("ws-a", started_at="2026-11-12T10:01:00+00:00"),
            make_record("ws-b", started_at="2026-11-12T10:02:00+00:00"),
        ]
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=(150, 30)) as pilot:
        await attached(app, pilot, "ws-a")
        app.controller.model_key = "kiro/gpt-5.6-luna"
        assert app.session_model_key == "kiro/gpt-5.6-luna"
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        assert app.session_model_key == ""
        app.switch_model("model-sonnet")
        await pilot.pause()
        (kind, command), *_ = client.stream_for("ws-b").sent
        assert kind == "command" and command.model_key == "model-sonnet"
        assert not client.stream_for("ws-a").sent
