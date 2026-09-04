"""Unit tests for the pre-run cost estimator."""
import pytest

from localize.cost_estimator import estimate_run_cost, format_estimate


class TestEstimateRunCost:
    """estimate_run_cost should produce a deterministic ballpark from key/locale counts."""

    def test_zero_keys_is_zero_cost(self):
        est = estimate_run_cost(
            num_keys=0, locale_codes=["de", "es"],
            translate_model="gpt-4o-mini", review_model="gpt-4o",
        )
        assert est.num_units == 0
        assert est.estimated_total_tokens == 0
        assert est.estimated_cost_usd == 0.0
        assert est.cost_complete is True

    def test_units_are_keys_times_locales(self):
        est = estimate_run_cost(
            num_keys=10, locale_codes=["de", "es", "fr"],
            translate_model="gpt-4o-mini", review_model="gpt-4o",
        )
        assert est.num_keys == 10
        assert est.num_locales == 3
        assert est.num_units == 30

    def test_tokens_scale_with_units_and_passes(self):
        """Both a translate pass and a review pass are counted per unit."""
        est = estimate_run_cost(
            num_keys=5, locale_codes=["de"],
            translate_model="gpt-4o-mini", review_model="gpt-4o",
            avg_prompt_tokens_per_string=100,
            avg_completion_tokens_per_string=20,
        )
        # 5 units * (100 in + 20 out) per pass, two passes (translate + review)
        assert est.estimated_prompt_tokens == 5 * 100 * 2
        assert est.estimated_completion_tokens == 5 * 20 * 2
        assert est.estimated_total_tokens == 5 * 120 * 2

    def test_cost_uses_shared_price_table(self):
        """Cost must match the usage_tracker pricing for the chosen models."""
        est = estimate_run_cost(
            num_keys=1_000, locale_codes=["de"],
            translate_model="gpt-4o-mini", review_model="gpt-4o",
            avg_prompt_tokens_per_string=100,
            avg_completion_tokens_per_string=20,
        )
        # translate: 1000 units * (100 in, 20 out) on gpt-4o-mini
        #   = 100k in * 0.15/1e6 + 20k out * 0.60/1e6 = 0.015 + 0.012 = 0.027
        # review:   1000 units * (100 in, 20 out) on gpt-4o
        #   = 100k in * 2.50/1e6 + 20k out * 10.00/1e6 = 0.25 + 0.20 = 0.45
        assert est.estimated_cost_usd == pytest.approx(0.027 + 0.45, rel=1e-6)
        assert est.cost_complete is True

    def test_same_model_for_both_passes(self):
        est = estimate_run_cost(
            num_keys=100, locale_codes=["de", "es"],
            translate_model="gpt-4o-mini", review_model="gpt-4o-mini",
            avg_prompt_tokens_per_string=50,
            avg_completion_tokens_per_string=10,
        )
        units = 200
        prompt = units * 50 * 2
        completion = units * 10 * 2
        expected = prompt / 1e6 * 0.15 + completion / 1e6 * 0.60
        assert est.estimated_cost_usd == pytest.approx(expected, rel=1e-6)

    def test_unknown_model_marks_cost_incomplete(self):
        est = estimate_run_cost(
            num_keys=10, locale_codes=["de"],
            translate_model="some-local-llama", review_model="some-local-llama",
        )
        assert est.cost_complete is False
        assert est.estimated_cost_usd is None
        # Tokens are still estimated even when price is unknown.
        assert est.estimated_total_tokens > 0

    def test_partial_pricing_is_incomplete(self):
        """If only one of the two models is priced, cost is incomplete."""
        est = estimate_run_cost(
            num_keys=10, locale_codes=["de"],
            translate_model="gpt-4o-mini", review_model="local-model",
        )
        assert est.cost_complete is False
        assert est.estimated_cost_usd is None

    def test_enabled_semantic_review_adds_one_pass_for_all_candidate_keys(self):
        est = estimate_run_cost(
            num_keys=5,
            locale_codes=["de"],
            translate_model="gpt-4o-mini",
            review_model="gpt-4o-mini",
            review_num_keys=8,
            semantic_review_model="gpt-5.6-terra",
            semantic_review_num_keys=8,
            avg_prompt_tokens_per_string=100,
            avg_completion_tokens_per_string=20,
        )

        assert est.num_units == 5
        assert est.review_units == 8
        assert est.semantic_review_units == 8
        assert est.estimated_prompt_tokens == (5 + 8 + 8) * 100
        assert est.estimated_completion_tokens == (5 + 8 + 8) * 20
        expected_translation = (500 / 1e6 * 0.15) + (100 / 1e6 * 0.60)
        expected_review = (800 / 1e6 * 0.15) + (160 / 1e6 * 0.60)
        expected_semantic = (800 / 1e6 * 2.00) + (160 / 1e6 * 12.00)
        assert est.estimated_cost_usd == pytest.approx(
            expected_translation + expected_review + expected_semantic,
            rel=1e-6,
        )

    def test_semantic_review_defaults_to_holistic_review_scope(self):
        est = estimate_run_cost(
            num_keys=2,
            review_num_keys=5,
            locale_codes=["de", "es"],
            translate_model="gpt-4o-mini",
            review_model="gpt-5.6-terra",
            semantic_review_model="gpt-5.4-mini",
        )

        assert est.num_units == 4
        assert est.review_units == 10
        assert est.semantic_review_units == 10

    def test_semantic_review_unknown_price_marks_total_incomplete(self):
        est = estimate_run_cost(
            num_keys=1,
            locale_codes=["de"],
            translate_model="gpt-4o-mini",
            review_model="gpt-4o-mini",
            semantic_review_model="local-semantic-model",
        )

        assert est.cost_complete is False
        assert est.estimated_cost_usd is None


class TestFormatEstimate:
    def test_format_is_human_readable(self):
        est = estimate_run_cost(
            num_keys=42, locale_codes=["de", "es"],
            translate_model="gpt-4o-mini", review_model="gpt-4o",
        )
        text = format_estimate(est)
        assert "42" in text
        assert "84" in text  # units
        assert "$" in text

    def test_format_handles_unknown_price(self):
        est = estimate_run_cost(
            num_keys=5, locale_codes=["de"],
            translate_model="local", review_model="local",
        )
        text = format_estimate(est)
        assert "n/a" in text.lower() or "unknown" in text.lower()

    def test_format_names_enabled_semantic_review_model(self):
        est = estimate_run_cost(
            num_keys=5,
            locale_codes=["de"],
            translate_model="gpt-4o-mini",
            review_model="gpt-5.6-terra",
            semantic_review_model="gpt-5.4-mini",
        )

        assert "semantic=gpt-5.4-mini" in format_estimate(est)

    def test_format_distinguishes_memory_hits_from_initial_translation(self):
        est = estimate_run_cost(
            num_keys=0,
            review_num_keys=1,
            locale_codes=["de"],
            translate_model="gpt-4o-mini",
            review_model="gpt-5.6-terra",
        )

        text = format_estimate(est)

        assert "0 uncached keys x 1 locales = 0 initial-translation units" in text
        assert "estimated model-pass units: translate=0, review=1" in text
        assert "0 changed keys" not in text

    def test_format_is_a_string_for_empty_run(self):
        est = estimate_run_cost(
            num_keys=0, locale_codes=[],
            translate_model="gpt-4o-mini", review_model="gpt-4o",
        )
        assert isinstance(format_estimate(est), str)
