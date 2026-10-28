"""Tests for Kiro config, credential, credits field and credit pricing (077 M1)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import msgspec
import pytest
from archie_shared.config import ConfigError
from archie_shared.events import LLMRequest, decode_event, encode_event
from archie_shared.kiro import (
    CreditLedger,
    credit_cost,
    credits_used_since,
    kiro_model_id,
    kiro_model_key,
    period_start,
)
from archie_shared.schemas import KiroCreditsConfig, load_nexus_config

CFG = KiroCreditsConfig(included=100, resets_on=1, rate_usd=0.02, overage_rate_usd=0.04)


# --- config ---------------------------------------------------------------


def test_kiro_config_defaults(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    kiro = load_nexus_config(path=cfg).kiro
    assert kiro.default_model == "gpt-5.6-luna"
    assert kiro.models == (
        "claude-opus-5.5",
        "claude-sonnet-5.5",
        "claude-haiku-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    )
    assert kiro.effort is None
    assert (kiro.credits.included, kiro.credits.resets_on) == (2000, 1)
    assert (kiro.credits.rate_usd, kiro.credits.overage_rate_usd) == (0.02, 0.04)


def test_kiro_config_custom(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "kiro:\n  default_model: a\n  models: [a, b]\n  effort: high\n"
        "  credits:\n    included: 10\n    resets_on: 15\n"
    )
    kiro = load_nexus_config(path=cfg).kiro
    assert (kiro.default_model, kiro.models, kiro.effort) == ("a", ("a", "b"), "high")
    assert (kiro.credits.included, kiro.credits.resets_on) == (10, 15)


@pytest.mark.parametrize(
    "body",
    [
        "kiro:\n  default_model: zzz\n",
        "kiro:\n  models: []\n  default_model: a\n",
        "kiro:\n  models: [a, '']\n  default_model: a\n",
        "kiro:\n  effort: turbo\n",
        "kiro:\n  credits:\n    included: -1\n",
        "kiro:\n  credits:\n    resets_on: 0\n",
        "kiro:\n  credits:\n    resets_on: 32\n",
        "kiro:\n  credits:\n    rate_usd: -0.1\n",
        "kiro:\n  credits:\n    overage_rate_usd: -0.1\n",
        "kiro:\n  typo: 1\n",
    ],
)
def test_kiro_config_invalid(tmp_path, body):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body)
    with pytest.raises(ConfigError, match="Validation error"):
        load_nexus_config(path=cfg)


def test_kiro_credential_type(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    from archie_shared.credentials import CREDENTIAL_TYPES, get_credential, set_credential

    assert CREDENTIAL_TYPES["kiro"].__struct_fields__ == ("api_key",)
    set_credential("kiro", {"api_key": "k-123"})
    assert get_credential("kiro").api_key == "k-123"


def test_model_keys():
    assert kiro_model_key("gpt-5.6-luna") == "kiro/gpt-5.6-luna"
    assert kiro_model_id("kiro/gpt-5.6-luna") == "gpt-5.6-luna"
    assert kiro_model_id("bedrock-x") is None
    assert kiro_model_id("kiro/") is None


# --- llm_request.credits ---------------------------------------------------


def _request(**kw) -> LLMRequest:
    base = {
        "id": "r1",
        "scope": None,
        "turn": 1,
        "iteration": 0,
        "model_key": "kiro/gpt-5.6-luna",
        "sent_at": "2026-10-01T10:00:00+00:00",
        "duration_ms": 10,
        "status": "completed",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "context_tokens": 0,
        "cost_usd": 0.02,
    }
    base.update(kw)
    return LLMRequest(**base)


def test_native_request_encodes_without_credits():
    raw = encode_event(_request())
    assert "credits" not in raw
    assert decode_event(raw).credits is msgspec.UNSET


def test_credits_roundtrip():
    raw = encode_event(_request(credits=1.25))
    assert json.loads(raw)["credits"] == 1.25
    assert decode_event(raw).credits == 1.25


def test_old_line_without_credits_decodes():
    raw = (
        '{"type":"llm_request","id":"r1","scope":null,"turn":1,"iteration":0,"model_key":"m",'
        '"sent_at":"now","duration_ms":1,"status":"completed","input_tokens":1,'
        '"output_tokens":1,"cache_read_tokens":0,"cache_write_tokens":0,"context_tokens":1,'
        '"cost_usd":0}'
    )
    assert decode_event(raw, persisted=True).credits is msgspec.UNSET


# --- pricing ---------------------------------------------------------------


def test_credit_cost_under_crossing_over():
    assert credit_cost(10, 0, CFG) == pytest.approx(0.2)
    assert credit_cost(10, 95, CFG) == pytest.approx(5 * 0.02 + 5 * 0.04)
    assert credit_cost(10, 200, CFG) == pytest.approx(0.4)
    assert credit_cost(0, 0, CFG) == 0.0


def test_period_start_day_1():
    now = datetime(2026, 10, 27, 15, tzinfo=UTC)
    assert period_start(now, 1) == datetime(2026, 10, 1, tzinfo=UTC)
    assert period_start(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 1) == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_period_start_clamps_to_month_length():
    # November has 30 days: resets_on 31 means the 30th.
    assert period_start(datetime(2026, 11, 30, 1, tzinfo=UTC), 31) == datetime(
        2026, 11, 30, tzinfo=UTC
    )
    assert period_start(datetime(2026, 11, 29, tzinfo=UTC), 31) == datetime(
        2026, 10, 31, tzinfo=UTC
    )
    assert period_start(datetime(2026, 1, 5, tzinfo=UTC), 15) == datetime(2025, 12, 15, tzinfo=UTC)


def test_ledger_resets_at_period_boundary():
    ledger = CreditLedger(CFG, used=95, start=datetime(2026, 10, 1, tzinfo=UTC))
    assert ledger.charge(10, datetime(2026, 10, 20, tzinfo=UTC)) == pytest.approx(0.3)
    assert ledger.used == 105
    assert ledger.charge(10, datetime(2026, 11, 2, tzinfo=UTC)) == pytest.approx(0.2)
    assert ledger.used == 10


def test_credits_used_since_scans_only_current_period(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    lines = [
        encode_event(_request(id="a", credits=2.5, sent_at="2026-10-02T00:00:00+00:00")),
        encode_event(_request(id="b", credits=4.0, sent_at="2026-09-30T23:59:00+00:00")),
        encode_event(_request(id="c")),  # native, no credits
        '{"type":"tool_result","content":"\\"credits\\": 99"}',
        encode_event(_request(id="d", credits=1.0, sent_at="2026-10-05T00:00:00+00:00")),
    ]
    (sessions / "s1.jsonl").write_text("\n".join(lines) + "\n")
    old = sessions / "s0.jsonl"
    old.write_text(encode_event(_request(id="e", credits=50.0)) + "\n")
    stamp = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    os.utime(old, (stamp, stamp))
    os.utime(sessions / "s1.jsonl", (start.timestamp() + 3600,) * 2)
    assert credits_used_since(sessions, start) == pytest.approx(3.5)
    assert credits_used_since(tmp_path / "missing", start) == 0.0
