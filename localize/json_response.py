"""Helpers for provider-neutral JSON chat responses."""

from __future__ import annotations

import json
from typing import Any


def chat_json_mode_kwargs(provider: Any, model: str) -> dict[str, dict[str, str]]:
    """Return JSON-mode request kwargs only when the provider route supports them."""
    capabilities = provider.capabilities_for_model(model)
    if getattr(capabilities, "supports_response_format", True):
        return {"response_format": {"type": "json_object"}}
    return {}


def chat_reasoning_effort_kwargs(
    provider: Any,
    model: str,
    reasoning_effort: str | None,
) -> dict[str, str]:
    """Return reasoning effort only when configured and supported by the route."""
    if not reasoning_effort:
        return {}
    capabilities = provider.capabilities_for_model(model)
    supported_efforts = getattr(capabilities, "supported_reasoning_efforts", frozenset())
    if (
        getattr(capabilities, "supports_reasoning_effort", False)
        and (not supported_efforts or reasoning_effort in supported_efforts)
    ):
        return {"reasoning_effort": reasoning_effort}
    return {}


def extract_json_object_text(response_text: str) -> str:
    """Extract the first balanced JSON object from raw or fenced model output."""
    stripped = response_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()

    start = stripped.find("{")
    if start < 0:
        return stripped

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start:index + 1]
    return stripped


def loads_json_object(response_text: str) -> dict[str, Any]:
    """Parse raw model output as a JSON object, accepting common fenced output."""
    parsed = json.loads(extract_json_object_text(response_text))
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object response.")
    return parsed
