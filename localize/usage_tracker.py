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
from dataclasses import dataclass
from typing import Any, Dict, Optional

from localize.atomic_io import write_json_atomic

# USD per 1,000,000 tokens. Verify against current OpenAI pricing before relying.
DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    "gpt-5.6": {
        "input": 5.00, "cached_input": 0.50, "cache_write": 6.25, "output": 30.00,
    },
    "gpt-5.6-sol": {
        "input": 5.00, "cached_input": 0.50, "cache_write": 6.25, "output": 30.00,
    },
    "gpt-5.6-terra": {
        "input": 2.50, "cached_input": 0.25, "cache_write": 3.125, "output": 15.00,
    },
    "gpt-5.6-luna": {
        "input": 1.00, "cached_input": 0.10, "cache_write": 1.25, "output": 6.00,
    },
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.4-pro": {"input": 30.00, "output": 180.00},
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "gpt-4o": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
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
    *,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
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
    cached_tokens = max(0, min(int(cached_tokens or 0), prompt_tokens))
    remaining_prompt_tokens = prompt_tokens - cached_tokens
    cache_write_tokens = max(
        0,
        min(int(cache_write_tokens or 0), remaining_prompt_tokens),
    )
    uncached_tokens = remaining_prompt_tokens - cache_write_tokens
    cached_input_price = price.get("cached_input", price["input"])
    cache_write_price = price.get("cache_write", price["input"])
    return (
        uncached_tokens / 1_000_000 * price["input"]
        + cached_tokens / 1_000_000 * cached_input_price
        + cache_write_tokens / 1_000_000 * cache_write_price
        + completion_tokens / 1_000_000 * price["output"]
    )


@dataclass
class _ModelUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


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

    def merge_summary(self, summary: Dict[str, Any]) -> None:
        """Merge a previously serialized usage summary into this tracker."""
        models = summary.get("models", {})
        if not isinstance(models, dict):
            raise ValueError("Usage summary models must be an object.")
        for model, raw_usage in models.items():
            if not isinstance(raw_usage, dict):
                raise ValueError(f"Usage summary for {model!r} must be an object.")
            entry = self._by_model.setdefault(str(model), _ModelUsage())
            entry.calls += int(raw_usage.get("calls", 0) or 0)
            entry.prompt_tokens += int(raw_usage.get("prompt_tokens", 0) or 0)
            entry.completion_tokens += int(raw_usage.get("completion_tokens", 0) or 0)
            entry.cached_tokens += int(raw_usage.get("cached_tokens", 0) or 0)
            entry.cache_write_tokens += int(raw_usage.get("cache_write_tokens", 0) or 0)

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Add one API call's token counts for ``model``."""
        entry = self._by_model.setdefault(model, _ModelUsage())
        entry.calls += 1
        entry.prompt_tokens += int(prompt_tokens or 0)
        entry.completion_tokens += int(completion_tokens or 0)
        entry.cached_tokens += int(cached_tokens or 0)
        entry.cache_write_tokens += int(cache_write_tokens or 0)

    def record_response(self, model: str, response: Any) -> None:
        """Record usage from an OpenAI ChatCompletion response (no-op if absent)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        self.record(
            model,
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            cached_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
            cache_write_tokens=getattr(prompt_details, "cache_write_tokens", 0) or 0,
        )

    def _model_cost(self, model: str, usage: _ModelUsage) -> Optional[float]:
        return cost_for_tokens(
            model,
            usage.prompt_tokens,
            usage.completion_tokens,
            self._prices,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    def summary(self) -> Dict[str, Any]:
        """Return a structured summary of usage and estimated cost."""
        models: Dict[str, Any] = {}
        total_prompt = total_completion = 0
        total_cost = 0.0
        cost_known = True
        for model, u in sorted(self._by_model.items()):
            cost = self._model_cost(model, u)
            if cost is None:
                cost_known = False
            else:
                total_cost += cost
            models[model] = {
                "calls": u.calls,
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "cached_tokens": u.cached_tokens,
                "cache_write_tokens": u.cache_write_tokens,
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

    def write_json(
        self,
        path: str,
        *,
        merge_existing: bool = False,
        stage_name: Optional[str] = None,
    ) -> None:
        """Write usage, optionally merging an earlier stage and stage subtotal."""
        current_summary = self.summary()
        payload = current_summary
        stages: Dict[str, Any] = {}
        if merge_existing:
            try:
                with open(path, "r", encoding="utf-8") as file:
                    existing_summary = json.load(file)
            except FileNotFoundError:
                existing_summary = {}
            combined = UsageTracker(prices=self._prices)
            combined.merge_summary(existing_summary)
            combined.merge_summary(current_summary)
            payload = combined.summary()
            raw_stages = existing_summary.get("stages", {})
            if isinstance(raw_stages, dict):
                stages = dict(raw_stages)
        if stage_name:
            stage = UsageTracker(prices=self._prices)
            if stage_name in stages:
                stage.merge_summary(stages[stage_name])
            stage.merge_summary(current_summary)
            stages[stage_name] = stage.summary()
        if stages:
            payload["stages"] = stages
        write_json_atomic(path, payload)


# Module-level singleton, consistent with the module-global style of the pipeline.
usage_tracker = UsageTracker()
