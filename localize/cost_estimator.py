"""Pre-run cost and scope estimation for a translation run.

Gives an OSS maintainer a ballpark of "how many strings, how many tokens, and
roughly how much will this cost on my key" *before* spending anything — the
piece a hosted SaaS hides behind word-count metering.

The estimate is deliberately rough: per-string token volumes vary with prompt
overhead, glossary size, and language. The defaults below are conservative
heuristics; treat the output as a ballpark, not a quote. Pricing comes from the
same table the live :mod:`usage_tracker` uses, so estimates and actuals are
comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence

from localize.usage_tracker import cost_for_tokens

# Rough per-string token heuristics (input includes amortized prompt/glossary
# context; output is the translated string). Override per call if you have data.
DEFAULT_AVG_PROMPT_TOKENS_PER_STRING = 220
DEFAULT_AVG_COMPLETION_TOKENS_PER_STRING = 40


@dataclass(frozen=True)
class CostEstimate:
    """A pre-run scope and cost estimate."""
    num_keys: int
    num_locales: int
    num_units: int  # keys * locales (one translation unit each)
    review_units: int
    translate_model: str
    review_model: str
    semantic_review_model: Optional[str]
    semantic_review_units: int
    estimated_prompt_tokens: int
    estimated_completion_tokens: int
    estimated_total_tokens: int
    estimated_cost_usd: Optional[float]  # None if any model's price is unknown
    cost_complete: bool


def estimate_run_cost(
    *,
    num_keys: int,
    locale_codes: Sequence[str],
    translate_model: str,
    review_model: str,
    review_num_keys: Optional[int] = None,
    semantic_review_model: Optional[str] = None,
    semantic_review_num_keys: Optional[int] = None,
    avg_prompt_tokens_per_string: int = DEFAULT_AVG_PROMPT_TOKENS_PER_STRING,
    avg_completion_tokens_per_string: int = DEFAULT_AVG_COMPLETION_TOKENS_PER_STRING,
    prices: Optional[Dict[str, Dict[str, float]]] = None,
) -> CostEstimate:
    """Estimate tokens and USD cost for translating ``num_keys`` into each locale.

    Each (key, locale) pair is one translation *unit*. ``review_num_keys`` may
    be larger than ``num_keys`` because translation-memory hits skip translation
    but still enter holistic review. When ``semantic_review_model`` is set,
    candidate keys also incur one semantic-review pass; its key count defaults
    to the holistic-review count. If any selected model has no known price, the
    overall cost is reported as ``None`` while token counts are still returned.
    """
    num_locales = len(locale_codes)
    num_units = max(0, int(num_keys)) * num_locales

    review_keys = num_keys if review_num_keys is None else review_num_keys
    review_units = max(0, int(review_keys)) * num_locales
    semantic_review_units = 0
    if semantic_review_model:
        semantic_keys = (
            review_keys
            if semantic_review_num_keys is None
            else semantic_review_num_keys
        )
        semantic_review_units = max(0, int(semantic_keys)) * num_locales

    translate_prompt_tokens = num_units * avg_prompt_tokens_per_string
    translate_completion_tokens = num_units * avg_completion_tokens_per_string
    review_prompt_tokens = review_units * avg_prompt_tokens_per_string
    review_completion_tokens = review_units * avg_completion_tokens_per_string
    semantic_prompt_tokens = semantic_review_units * avg_prompt_tokens_per_string
    semantic_completion_tokens = semantic_review_units * avg_completion_tokens_per_string

    estimated_prompt_tokens = (
        translate_prompt_tokens + review_prompt_tokens + semantic_prompt_tokens
    )
    estimated_completion_tokens = (
        translate_completion_tokens
        + review_completion_tokens
        + semantic_completion_tokens
    )

    translate_cost = cost_for_tokens(
        translate_model,
        translate_prompt_tokens,
        translate_completion_tokens,
        prices,
        apply_long_context_pricing=False,
    )
    review_cost = cost_for_tokens(
        review_model,
        review_prompt_tokens,
        review_completion_tokens,
        prices,
        apply_long_context_pricing=False,
    )
    semantic_review_cost = (
        cost_for_tokens(
            semantic_review_model,
            semantic_prompt_tokens,
            semantic_completion_tokens,
            prices,
            apply_long_context_pricing=False,
        )
        if semantic_review_model
        else 0.0
    )

    if translate_cost is None or review_cost is None or semantic_review_cost is None:
        total_cost: Optional[float] = None
        cost_complete = False
    else:
        total_cost = round(translate_cost + review_cost + semantic_review_cost, 6)
        cost_complete = True

    return CostEstimate(
        num_keys=int(num_keys),
        num_locales=num_locales,
        num_units=num_units,
        review_units=review_units,
        translate_model=translate_model,
        review_model=review_model,
        semantic_review_model=semantic_review_model,
        semantic_review_units=semantic_review_units,
        estimated_prompt_tokens=estimated_prompt_tokens,
        estimated_completion_tokens=estimated_completion_tokens,
        estimated_total_tokens=estimated_prompt_tokens + estimated_completion_tokens,
        estimated_cost_usd=total_cost,
        cost_complete=cost_complete,
    )


def format_estimate(estimate: CostEstimate) -> str:
    """Human-readable multi-line estimate for logging before a run."""
    if estimate.estimated_cost_usd is None:
        cost_str = "n/a (no price set for one or more models)"
    else:
        cost_str = f"~${estimate.estimated_cost_usd:.4f}"
    model_summary = (
        f"translate={estimate.translate_model}, review={estimate.review_model}, "
        f"semantic={estimate.semantic_review_model}"
        if estimate.semantic_review_model
        else f"translate={estimate.translate_model}, review={estimate.review_model}"
    )
    pass_summary = f"translate={estimate.num_units:,}, review={estimate.review_units:,}"
    if estimate.semantic_review_model:
        pass_summary += f", semantic={estimate.semantic_review_units:,}"
    return "\n".join([
        "===== Estimated scope & cost (pre-run) =====",
        f"  {estimate.num_keys:,} uncached keys x {estimate.num_locales} locales "
        f"= {estimate.num_units:,} initial-translation units",
        f"  models: {model_summary}",
        f"  estimated model-pass units: {pass_summary}",
        f"  est. tokens: {estimate.estimated_total_tokens:,} "
        f"({estimate.estimated_prompt_tokens:,} in + {estimate.estimated_completion_tokens:,} out)",
        f"  est. cost: {cost_str}  (rough ballpark — actuals reported after the run)",
    ])
