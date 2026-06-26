"""Unit tests for the per-run token usage / cost tracker."""

import json
from types import SimpleNamespace

from src.usage_tracker import UsageTracker


PRICES = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}


def test_record_and_totals():
    t = UsageTracker(prices=PRICES)
    t.record("gpt-4o-mini", 1000, 500)
    t.record("gpt-4o-mini", 1000, 500)
    s = t.summary()
    assert s["models"]["gpt-4o-mini"]["calls"] == 2
    assert s["models"]["gpt-4o-mini"]["prompt_tokens"] == 2000
    assert s["models"]["gpt-4o-mini"]["completion_tokens"] == 1000
    assert s["totals"]["total_tokens"] == 3000


def test_cost_calculation():
    t = UsageTracker(prices=PRICES)
    # 1,000,000 input + 1,000,000 output on gpt-4o = $2.50 + $10.00 = $12.50
    t.record("gpt-4o", 1_000_000, 1_000_000)
    s = t.summary()
    assert s["models"]["gpt-4o"]["estimated_cost_usd"] == 12.5
    assert s["totals"]["estimated_cost_usd"] == 12.5
    assert s["totals"]["cost_complete"] is True


def test_multiple_models_aggregate():
    t = UsageTracker(prices=PRICES)
    t.record("gpt-4o-mini", 1_000_000, 0)   # $0.15
    t.record("gpt-4o", 1_000_000, 0)         # $2.50
    s = t.summary()
    assert s["totals"]["calls"] == 2
    assert round(s["totals"]["estimated_cost_usd"], 4) == 2.65


def test_unknown_model_tracks_tokens_but_no_cost():
    t = UsageTracker(prices=PRICES)
    t.record("some-future-model", 1000, 1000)
    s = t.summary()
    assert s["models"]["some-future-model"]["total_tokens"] == 2000
    assert s["models"]["some-future-model"]["estimated_cost_usd"] is None
    assert s["totals"]["cost_complete"] is False


def test_provider_prefixed_openai_model_uses_bare_model_price():
    t = UsageTracker(prices=PRICES)
    t.record("openai:gpt-4o", 1_000_000, 1_000_000)
    s = t.summary()
    assert s["models"]["openai:gpt-4o"]["estimated_cost_usd"] == 12.5
    assert s["totals"]["cost_complete"] is True


def test_unknown_provider_prefixed_model_does_not_use_bare_model_price():
    t = UsageTracker(prices=PRICES)
    t.record("azure:gpt-4o", 1_000_000, 1_000_000)
    s = t.summary()
    assert s["models"]["azure:gpt-4o"]["estimated_cost_usd"] is None
    assert s["totals"]["cost_complete"] is False


def test_record_response_reads_usage():
    t = UsageTracker(prices=PRICES)
    resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30))
    t.record_response("gpt-4o-mini", resp)
    s = t.summary()
    assert s["models"]["gpt-4o-mini"]["prompt_tokens"] == 120
    assert s["models"]["gpt-4o-mini"]["completion_tokens"] == 30


def test_record_response_handles_missing_usage():
    t = UsageTracker(prices=PRICES)
    t.record_response("gpt-4o-mini", SimpleNamespace(usage=None))
    t.record_response("gpt-4o-mini", SimpleNamespace())  # no usage attr at all
    assert t.summary()["totals"]["total_tokens"] == 0


def test_reset():
    t = UsageTracker(prices=PRICES)
    t.record("gpt-4o", 100, 100)
    t.reset()
    assert t.summary()["totals"]["total_tokens"] == 0
    assert t.summary()["models"] == {}


def test_format_summary_contains_figures():
    t = UsageTracker(prices=PRICES)
    t.record("gpt-4o", 1_000_000, 1_000_000)
    out = t.format_summary()
    assert "gpt-4o" in out
    assert "TOTAL" in out
    assert "12.5" in out


def test_format_summary_empty():
    t = UsageTracker(prices=PRICES)
    assert "No API calls" in t.format_summary()


def test_write_json(tmp_path):
    t = UsageTracker(prices=PRICES)
    t.record("gpt-4o-mini", 500, 250)
    path = tmp_path / "nested" / "usage.json"
    t.write_json(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["totals"]["total_tokens"] == 750
    assert data["models"]["gpt-4o-mini"]["calls"] == 1
