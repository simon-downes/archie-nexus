import json
import math
from datetime import UTC, datetime, timedelta

import pytest
from archie_agent.exec.tools.cache import ToolCache


def test_cache_round_trip_and_expiry(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    current = [now]
    cache = ToolCache(tmp_path, lambda: current[0])
    cache.set("google.labels", {"labels": [{"id": "INBOX", "name": "Inbox"}]}, 60)
    assert cache.get("google.labels")["labels"][0]["id"] == "INBOX"
    current[0] += timedelta(seconds=60)
    assert cache.get("google.labels") is None


def test_cache_rejects_unsafe_keys_and_values(tmp_path):
    cache = ToolCache(tmp_path)
    with pytest.raises(ValueError):
        cache.set("../escape", {}, 60)
    with pytest.raises(ValueError):
        cache.set("google.labels", [], 60)
    with pytest.raises(ValueError):
        cache.set("google.labels", {}, 0)


def test_cache_corrupt_entry_is_miss(tmp_path):
    path = tmp_path / "google.labels.json"
    path.write_text("not json")
    assert ToolCache(tmp_path).get("google.labels") is None


def test_cache_requires_exact_envelope_and_timezone(tmp_path):
    path = tmp_path / "google.labels.json"
    path.write_text(json.dumps({"expires": "2030-01-01T00:00:00+00:00", "data": {}, "extra": 1}))
    cache = ToolCache(tmp_path)
    assert cache.get("google.labels") is None
    path.write_text(json.dumps({"expires": "2030-01-01T00:00:00", "data": {}}))
    assert cache.get("google.labels") is None


def test_cache_rejects_nonfinite_values_and_ttl(tmp_path):
    cache = ToolCache(tmp_path)
    with pytest.raises((ValueError, OverflowError)):
        cache.set("google.labels", {"value": math.nan}, 60)
    with pytest.raises(ValueError):
        cache.set("google.labels", {}, math.inf)


def test_cache_enforces_serialized_size(tmp_path):
    with pytest.raises(ValueError):
        ToolCache(tmp_path).set("google.labels", {"value": "x" * (256 * 1024)}, 60)


def test_cache_exists_valid_and_del(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    current = [now]
    cache = ToolCache(tmp_path, lambda: current[0])
    assert cache.exists("google.labels") is False
    cache.set("google.labels", {"labels": []}, 20)
    assert cache.exists("google.labels") is True
    assert cache.valid("google.labels") is True
    current[0] += timedelta(seconds=10)
    assert cache.valid("google.labels") is False
    getattr(cache, "del")("google.labels")
    assert cache.exists("google.labels") is False


def test_cache_uses_flat_bounded_keys(tmp_path):
    cache = ToolCache(tmp_path)
    with pytest.raises(ValueError):
        cache.set("google." + "x" * 65, {}, 60)
    with pytest.raises(ValueError):
        cache.set("google.labels.extra", {}, 60)
