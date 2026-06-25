"""Compatibility helpers for OpenAI-compatible chat completion clients."""

from __future__ import annotations

import logging
from typing import Any, Literal

from openai import OpenAIError

CompletionTokenParameter = Literal["max_tokens", "max_completion_tokens"]

logger = logging.getLogger(__name__)

_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "o5",
)

_UNSUPPORTED_PARAMETER_MARKERS = (
    "unsupported parameter",
    "not supported",
    "not compatible",
    "unrecognized request argument",
    "unknown parameter",
    "unexpected keyword argument",
    "invalid parameter",
)


def completion_token_parameter_for_model(model: str) -> CompletionTokenParameter:
    """Return the preferred completion-token cap parameter for ``model``.

    OpenAI reasoning/newer chat models reject ``max_tokens`` and require
    ``max_completion_tokens``. Other OpenAI-compatible providers may only
    support the older name, so unknown models keep ``max_tokens`` first and the
    request helper falls back when the API returns a parameter-compatibility
    error.
    """
    normalized = model.lower()
    if normalized.startswith(_MAX_COMPLETION_TOKEN_MODEL_PREFIXES):
        return "max_completion_tokens"
    return "max_tokens"


def _looks_like_unsupported_parameter(exc: Exception, parameter: str) -> bool:
    message = str(exc).lower()
    if parameter.lower() not in message:
        return False
    return any(marker in message for marker in _UNSUPPORTED_PARAMETER_MARKERS)


def _with_completion_token_limit(
    kwargs: dict[str, Any],
    parameter: CompletionTokenParameter,
    limit: int,
) -> dict[str, Any]:
    request_kwargs = dict(kwargs)
    request_kwargs.pop("max_tokens", None)
    request_kwargs.pop("max_completion_tokens", None)
    request_kwargs[parameter] = limit
    return request_kwargs


async def create_chat_completion(
    client: Any,
    *,
    model: str,
    messages: Any,
    completion_token_limit: int | None = None,
    **kwargs: Any,
) -> Any:
    """Create a chat completion with model-aware token-limit parameters.

    ``completion_token_limit`` is intentionally provider-agnostic. The helper
    chooses the preferred OpenAI parameter name for known models and retries
    once with the alternate name if an OpenAI-compatible API rejects it.
    """
    if completion_token_limit is None:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs,
        )

    first_parameter = completion_token_parameter_for_model(model)
    fallback_parameter: CompletionTokenParameter = (
        "max_completion_tokens"
        if first_parameter == "max_tokens"
        else "max_tokens"
    )
    first_kwargs = _with_completion_token_limit(
        kwargs,
        first_parameter,
        completion_token_limit,
    )

    try:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            **first_kwargs,
        )
    except (OpenAIError, TypeError) as exc:
        if not _looks_like_unsupported_parameter(exc, first_parameter):
            raise

    logger.debug(
        "Retrying chat completion for model '%s' with '%s' instead of '%s' "
        "after parameter compatibility error.",
        model,
        fallback_parameter,
        first_parameter,
    )
    fallback_kwargs = _with_completion_token_limit(
        kwargs,
        fallback_parameter,
        completion_token_limit,
    )
    return await client.chat.completions.create(
        model=model,
        messages=messages,
        **fallback_kwargs,
    )
