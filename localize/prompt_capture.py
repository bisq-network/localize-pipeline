"""Deterministic, no-network chat provider for prompt regression snapshots."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Deque, Dict, Iterable, List, Optional

from localize.cost_estimator import CostEstimate, estimate_run_cost, format_estimate
from localize.model_provider import ModelProviderCapabilities


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump())
    raise TypeError(f"Prompt capture cannot serialize {type(value).__name__}.")


class PromptCaptureProvider:
    """Record canonical request payloads and return scripted responses."""

    client = None

    def __init__(self, *, stage: str, responses: Iterable[str]) -> None:
        normalized_stage = str(stage).strip()
        if normalized_stage not in {"draft", "review"}:
            raise ValueError("Prompt capture stage must be 'draft' or 'review'.")
        self.stage = normalized_stage
        self._responses: Deque[str] = deque(str(response) for response in responses)
        self._payloads: List[Dict[str, Any]] = []

    async def create_chat_completion(
        self,
        *,
        model: str,
        messages: Any,
        completion_token_limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        if not self._responses:
            raise RuntimeError(f"No scripted {self.stage} capture response remains.")
        payload: Dict[str, Any] = {
            "model": model,
            "messages": _json_safe(messages),
            "request": _json_safe(kwargs),
        }
        if completion_token_limit is not None:
            payload["completion_token_limit"] = completion_token_limit
        self._payloads.append(payload)
        response_text = self._responses.popleft()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=response_text),
                )
            ]
        )

    def canonical_payloads(self) -> List[Dict[str, Any]]:
        """Return JSON-safe request payloads in dispatch order."""
        return json.loads(self.canonical_json())

    def canonical_json(self) -> str:
        """Return canonical JSON suitable for checked-in snapshot comparison."""
        return json.dumps(
            self._payloads,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def write_capture(self, output_dir: str | Path) -> Path:
        """Write this stage to its own stable capture artifact."""
        destination = Path(output_dir) / f"{self.stage}-requests.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                self.canonical_payloads(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return destination

    def count_tokens(self, text: str, model_name: str) -> int:
        del model_name
        return len(str(text).split())

    def estimate_run_cost(
        self,
        *,
        num_keys: int,
        locale_codes: Sequence[str],
        translate_model: str,
        review_model: str,
        avg_prompt_tokens_per_string: int = 220,
        avg_completion_tokens_per_string: int = 40,
    ) -> CostEstimate:
        return estimate_run_cost(
            num_keys=num_keys,
            locale_codes=locale_codes,
            translate_model=translate_model,
            review_model=review_model,
            avg_prompt_tokens_per_string=avg_prompt_tokens_per_string,
            avg_completion_tokens_per_string=avg_completion_tokens_per_string,
        )

    def format_estimate(self, estimate: CostEstimate) -> str:
        return format_estimate(estimate)

    def record_response(self, model: str, response: Any) -> None:
        del model, response

    def write_usage_summary(
        self,
        path: str,
        *,
        merge_existing: bool = False,
        stage_name: Optional[str] = None,
    ) -> None:
        del merge_existing, stage_name
        Path(path).write_text(
            '{"calls":[],"models":{},"totals":{"calls":0}}\n',
            encoding="utf-8",
        )

    def format_usage_summary(self) -> str:
        return "Prompt capture made no model API calls."

    def is_retryable_error(self, exc: Exception) -> bool:
        del exc
        return False

    def capabilities_for_model(self, model: str) -> ModelProviderCapabilities:
        del model
        return ModelProviderCapabilities(
            provider_key="capture",
            supports_response_format=True,
            supports_completion_token_limit=True,
        )
