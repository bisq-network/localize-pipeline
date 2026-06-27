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

# The pipeline runs two LLM passes per string: fast translate + holistic review.
_PASSES = 2


@dataclass(frozen=True)
class CostEstimate:
    """A pre-run scope and cost estimate."""
    num_keys: int
    num_locales: int
    num_units: int  # keys * locales (one translation unit each)
    translate_model: str
    review_model: str
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
    avg_prompt_tokens_per_string: int = DEFAULT_AVG_PROMPT_TOKENS_PER_STRING,
    avg_completion_tokens_per_string: int = DEFAULT_AVG_COMPLETION_TOKENS_PER_STRING,
    prices: Optional[Dict[str, Dict[str, float]]] = None,
) -> CostEstimate:
    """Estimate tokens and USD cost for translating ``num_keys`` into each locale.

    Each (key, locale) pair is one *unit*; every unit incurs a translate pass and
    a review pass. Translate-pass tokens are priced on ``translate_model`` and
    review-pass tokens on ``review_model``. If either model has no known price the
    overall cost is reported as ``None`` (``cost_complete=False``) while token
    counts are still returned.
    """
    num_locales = len(locale_codes)
    num_units = max(0, int(num_keys)) * num_locales

    prompt_per_pass = num_units * avg_prompt_tokens_per_string
    completion_per_pass = num_units * avg_completion_tokens_per_string

    estimated_prompt_tokens = prompt_per_pass * _PASSES
    estimated_completion_tokens = completion_per_pass * _PASSES

    translate_cost = cost_for_tokens(
        translate_model, prompt_per_pass, completion_per_pass, prices
    )
    review_cost = cost_for_tokens(
        review_model, prompt_per_pass, completion_per_pass, prices
    )

    if translate_cost is None or review_cost is None:
        total_cost: Optional[float] = None
        cost_complete = False
    else:
        total_cost = round(translate_cost + review_cost, 6)
        cost_complete = True

    return CostEstimate(
        num_keys=int(num_keys),
        num_locales=num_locales,
        num_units=num_units,
        translate_model=translate_model,
        review_model=review_model,
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
    return "\n".join([
        "===== Estimated scope & cost (pre-run) =====",
        f"  {estimate.num_keys:,} changed keys x {estimate.num_locales} locales "
        f"= {estimate.num_units:,} translation units",
        f"  models: translate={estimate.translate_model}, review={estimate.review_model}",
        f"  est. tokens: {estimate.estimated_total_tokens:,} "
        f"({estimate.estimated_prompt_tokens:,} in + {estimate.estimated_completion_tokens:,} out)",
        f"  est. cost: {cost_str}  (rough ballpark — actuals reported after the run)",
    ])
