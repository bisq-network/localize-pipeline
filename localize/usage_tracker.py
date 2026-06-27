"""Per-run OpenAI token usage and cost tracking.

Accumulates ``prompt``/``completion`` token counts per model across a single
pipeline run and produces an estimated USD cost using a configurable price
table. The numbers are written to ``logs/token_usage_summary.json`` and logged
at the end of a run so real cost-per-run can be compared against estimates.

Prices are USD per 1,000,000 tokens and are editable below. They change over
time and by model — update ``DEFAULT_PRICES`` (or pass ``prices=`` to the
constructor) and treat reported cost as an estimate, not a billing figure.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

# USD per 1,000,000 tokens. Verify against current OpenAI pricing before relying.
DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
}
_PRICE_COMPATIBLE_PROVIDER_PREFIXES = frozenset({"openai"})


def _price_lookup_model(model: str) -> str:
    provider, separator, bare_model = model.partition(":")
    if separator and provider in _PRICE_COMPATIBLE_PROVIDER_PREFIXES:
        return bare_model
    return model


def cost_for_tokens(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: Optional[Dict[str, Dict[str, float]]] = None,
) -> Optional[float]:
    """USD cost for token counts, or ``None`` if ``model`` has no known price.

    Single source of truth for the pricing math, shared by the live usage
    tracker and the pre-run cost estimator.
    """
    table = prices if prices is not None else DEFAULT_PRICES
    price = table.get(model)
    if price is None:
        price = table.get(_price_lookup_model(model))
    if price is None:
        return None
    return (
        prompt_tokens / 1_000_000 * price["input"]
        + completion_tokens / 1_000_000 * price["output"]
    )


@dataclass
class _ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class UsageTracker:
    """Thread-naive accumulator for token usage across a run.

    Safe for asyncio use because :meth:`record` performs no ``await`` and runs
    to completion on the single event-loop thread.
    """

    def __init__(self, prices: Optional[Dict[str, Dict[str, float]]] = None) -> None:
        self._prices = prices if prices is not None else DEFAULT_PRICES
        self._by_model: Dict[str, _ModelUsage] = {}

    def reset(self) -> None:
        self._by_model = {}

    def record(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Add one API call's token counts for ``model``."""
        entry = self._by_model.setdefault(model, _ModelUsage())
        entry.calls += 1
        entry.prompt_tokens += int(prompt_tokens or 0)
        entry.completion_tokens += int(completion_tokens or 0)

    def record_response(self, model: str, response: Any) -> None:
        """Record usage from an OpenAI ChatCompletion response (no-op if absent)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.record(
            model,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )

    def _model_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        return cost_for_tokens(model, prompt_tokens, completion_tokens, self._prices)

    def summary(self) -> Dict[str, Any]:
        """Return a structured summary of usage and estimated cost."""
        models: Dict[str, Any] = {}
        total_prompt = total_completion = 0
        total_cost = 0.0
        cost_known = True
        for model, u in sorted(self._by_model.items()):
            cost = self._model_cost(model, u.prompt_tokens, u.completion_tokens)
            if cost is None:
                cost_known = False
            else:
                total_cost += cost
            models[model] = {
                "calls": u.calls,
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.prompt_tokens + u.completion_tokens,
                "estimated_cost_usd": round(cost, 6) if cost is not None else None,
            }
            total_prompt += u.prompt_tokens
            total_completion += u.completion_tokens
        return {
            "models": models,
            "totals": {
                "calls": sum(u.calls for u in self._by_model.values()),
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "estimated_cost_usd": round(total_cost, 6),
                "cost_complete": cost_known,
            },
        }

    def format_summary(self) -> str:
        """Human-readable multi-line summary for logging."""
        s = self.summary()
        t = s["totals"]
        lines = ["===== Token usage this run ====="]
        if not s["models"]:
            lines.append("No API calls recorded.")
            return "\n".join(lines)
        for model, m in s["models"].items():
            cost = m["estimated_cost_usd"]
            cost_str = f"${cost:.4f}" if cost is not None else "n/a (no price set)"
            lines.append(
                f"  {model}: {m['calls']} calls, "
                f"{m['prompt_tokens']:,} in + {m['completion_tokens']:,} out "
                f"= {m['total_tokens']:,} tokens, est. {cost_str}"
            )
        note = "" if t["cost_complete"] else "  (incomplete — some models had no price set)"
        lines.append(
            f"  TOTAL: {t['calls']} calls, {t['total_tokens']:,} tokens, "
            f"est. ${t['estimated_cost_usd']:.4f}{note}"
        )
        return "\n".join(lines)

    def write_json(self, path: str) -> None:
        """Write the summary to ``path`` as JSON (creates parent dirs)."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, ensure_ascii=False, indent=2)


# Module-level singleton, consistent with the module-global style of the pipeline.
usage_tracker = UsageTracker()
