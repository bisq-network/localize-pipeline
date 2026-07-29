"""Unit tests for the per-run token usage / cost tracker."""

import json
from types import SimpleNamespace

from localize.usage_tracker import DEFAULT_PRICES, UsageTracker, cost_for_tokens


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


def test_default_prices_cover_configured_gpt5_models():
    assert DEFAULT_PRICES["gpt-5.6"] == {
        "input": 5.00,
        "cached_input": 0.50,
        "cache_write": 6.25,
        "output": 30.00,
    }
    assert DEFAULT_PRICES["gpt-5.6-sol"] == {
        "input": 5.00,
        "cached_input": 0.50,
        "cache_write": 6.25,
        "output": 30.00,
    }
    assert DEFAULT_PRICES["gpt-5.6-terra"] == {
        "input": 2.50,
        "cached_input": 0.25,
        "cache_write": 3.125,
        "output": 15.00,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
    }
    assert DEFAULT_PRICES["gpt-5.6-luna"] == {
        "input": 1.00,
        "cached_input": 0.10,
        "cache_write": 1.25,
        "output": 6.00,
    }
    assert DEFAULT_PRICES["gpt-5.5"] == {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    }
    assert DEFAULT_PRICES["gpt-5.4"] == {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    }
    assert DEFAULT_PRICES["gpt-5.4-mini"] == {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    }
    assert DEFAULT_PRICES["gpt-5.4-nano"] == {
        "input": 0.20,
        "cached_input": 0.02,
        "output": 1.25,
    }
    assert DEFAULT_PRICES["gpt-5.4-pro"] == {"input": 30.00, "output": 180.00}


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


def test_record_response_prices_cached_reads_and_writes_separately():
    t = UsageTracker(prices=DEFAULT_PRICES)
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=400_000,
                cache_write_tokens=200_000,
            ),
        )
    )

    t.record_response("gpt-5.6-terra", resp)

    model = t.summary()["models"]["gpt-5.6-terra"]
    assert model["cached_tokens"] == 400_000
    assert model["cache_write_tokens"] == 200_000
    # The 1M-token prompt uses Terra's long-context rates for the full request.
    assert model["estimated_cost_usd"] == 5.7


def test_gpt56_long_context_pricing_boundary():
    base_cost = cost_for_tokens(
        "gpt-5.6-terra",
        272_000,
        100_000,
        cached_tokens=100_000,
        cache_write_tokens=50_000,
    )
    long_cost = cost_for_tokens(
        "gpt-5.6-terra",
        272_001,
        100_000,
        cached_tokens=100_000,
        cache_write_tokens=50_000,
    )

    assert base_cost == 1.98625
    assert long_cost == 3.222505


def test_long_context_pricing_is_applied_per_call_not_aggregate():
    tracker = UsageTracker(prices=DEFAULT_PRICES)

    tracker.record("gpt-5.6-terra", 200_000, 10_000)
    tracker.record("gpt-5.6-terra", 200_000, 10_000)

    model = tracker.summary()["models"]["gpt-5.6-terra"]
    assert model["prompt_tokens"] == 400_000
    assert model["estimated_cost_usd"] == 1.3


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


def test_write_json_can_merge_semantic_review_usage(tmp_path):
    path = tmp_path / "usage.json"
    translation = UsageTracker(prices=DEFAULT_PRICES)
    translation.record("gpt-4o-mini", 1000, 100)
    translation.record("gpt-5.6-terra", 500, 50)
    translation.write_json(
        str(path),
        stage_name="translation_and_holistic_review",
    )

    semantic_review = UsageTracker(prices=DEFAULT_PRICES)
    semantic_review.record("gpt-5.4-mini", 300, 30)
    semantic_review.record("gpt-5.4-mini", 200, 20)
    semantic_review.write_json(
        str(path),
        merge_existing=True,
        stage_name="semantic_review",
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["models"]["gpt-4o-mini"]["calls"] == 1
    assert data["models"]["gpt-5.6-terra"]["calls"] == 1
    assert data["models"]["gpt-5.4-mini"]["calls"] == 2
    assert data["totals"]["calls"] == 4
    assert data["totals"]["total_tokens"] == 2200
    assert data["totals"]["cost_complete"] is True
    assert data["stages"]["translation_and_holistic_review"]["totals"]["calls"] == 2
    assert data["stages"]["semantic_review"]["totals"]["calls"] == 2
    assert data["stages"]["semantic_review"]["totals"]["total_tokens"] == 550
