"""Subagent tiers: the work each tier is for, and checks on the tier-to-model maps.

A caller picks a tier for each delegated task; ``agent.subagents.<backend>`` maps the
tier to a model. The guidance below is the only text that tells callers which tier
fits, on both the native ``task`` tool and the Kiro worker agents.
"""

from __future__ import annotations

from collections.abc import Mapping

from archie_shared.schemas import SUBAGENT_TIERS

DEFAULT_TIER = "standard"

TIER_GUIDANCE: dict[str, str] = {
    "light": "mechanical work: run commands, look things up, read or survey files and "
    "summarise them",
    "standard": "a well-scoped change, or an investigation with a clear question",
    "deep": "reviews, design judgement, debugging across modules, or work where a wrong "
    "answer is costly",
}


def unknown_native_tiers(tiers: Mapping[str, str], catalog: Mapping[str, object]) -> list[str]:
    """Return one error per native tier whose model key is not in the catalogue."""
    return [
        f"agent.subagents.native.{tier}: unknown model '{tiers[tier]}'"
        for tier in SUBAGENT_TIERS
        if tiers[tier] not in catalog
    ]
