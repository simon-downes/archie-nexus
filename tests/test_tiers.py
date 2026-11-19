"""Subagent tier guidance and the native tier-model startup check (095)."""

import logging

import msgspec
import pytest
from archie_agent import app as app_module
from archie_agent.tiers import DEFAULT_TIER, TIER_GUIDANCE, unknown_native_tiers
from archie_shared.models import DEFAULT_MODELS
from archie_shared.schemas import (
    SUBAGENT_TIERS,
    AgentConfig,
    NativeSubagentTiers,
    NexusConfig,
    SubagentsConfig,
)


def test_every_tier_has_guidance_and_the_default_is_a_tier():
    assert tuple(TIER_GUIDANCE) == SUBAGENT_TIERS
    assert DEFAULT_TIER in SUBAGENT_TIERS


def test_default_native_tiers_are_catalogue_keys():
    tiers = msgspec.structs.asdict(NativeSubagentTiers())
    assert unknown_native_tiers(tiers, DEFAULT_MODELS) == []


def test_unknown_native_tier_is_reported_by_config_key():
    tiers = {"light": "a", "standard": "a", "deep": "nope"}
    assert unknown_native_tiers(tiers, {"a": object()}) == [
        "agent.subagents.native.deep: unknown model 'nope'"
    ]


async def test_native_startup_exits_on_unknown_tier_model(caplog):
    config = NexusConfig(
        agent=AgentConfig(subagents=SubagentsConfig(native=NativeSubagentTiers(deep="nope")))
    )
    caplog.set_level(logging.ERROR)
    with pytest.raises(SystemExit) as exc:
        async with app_module._backend_lifespan("native", config):
            pass
    assert exc.value.code == 1
    assert "agent.subagents.native.deep: unknown model 'nope'" in caplog.text
