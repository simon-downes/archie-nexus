"""Tests for billable usage and derived session context tracking."""

import pytest
from archie_agent.session import Session
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry


def _session(context: int = 1_000, threshold: float = 0.8) -> Session:
    model = ModelEntry(
        name="Test",
        context=context,
        context_warning_threshold=threshold,
        provider=BedrockProvider(model_id="test"),
        cost=CostConfig(input=2.0, output=4.0, cache_read=0.5, cache_write=1.0),
    )
    return Session(model_id="test", model=model)


def test_context_pct_uses_total_context_input_not_billable_input():
    session = _session()
    session.add_turn(role="assistant", content="answer", output_tokens=20)
    session.record_usage(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=5,
    )

    assert session._last_input_tokens == 135
    assert session.context_pct == pytest.approx(15.5)
    assert session.total_input_tokens == 100
    assert session.total_cache_read_tokens == 30
    assert session.total_cache_write_tokens == 5


def test_context_warning_uses_derived_total_context_input():
    session = _session(context=150, threshold=0.8)
    session.record_usage(
        input_tokens=70,
        output_tokens=0,
        cache_read_tokens=30,
        cache_write_tokens=25,
    )

    assert session.context_warning is True
