"""Compatibility helpers for OpenAI chat-completion parameter drift."""

from __future__ import annotations

from typing import Any, Dict, Optional

from openai import BadRequestError


_MAX_TOKENS_PARAM = "max_tokens"
_MAX_COMPLETION_TOKENS_PARAM = "max_completion_tokens"
_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
)


def _model_without_provider(model: str) -> str:
    """Return the provider-local model name from either plain or provider:model form."""
    return str(model).split(":", 1)[-1].strip().lower()


def prefers_max_completion_tokens(model: str) -> bool:
    """Return true for model families that reject legacy max_tokens."""
    normalized_model = _model_without_provider(model)
    return normalized_model.startswith(_MAX_COMPLETION_TOKEN_MODEL_PREFIXES)


def chat_completion_token_limit_kwargs(model: str, token_limit: Optional[int]) -> Dict[str, int]:
    """Build the token-limit kwargs for a chat completion request.

    Newer OpenAI reasoning/chat model families reject ``max_tokens`` and require
    ``max_completion_tokens``. Older OpenAI-compatible providers often still expect
    ``max_tokens``. The caller can still recover if this guess is wrong by using
    ``create_chat_completion_with_token_limit``.
    """
    if token_limit is None:
        return {}
    param = (
        _MAX_COMPLETION_TOKENS_PARAM
        if prefers_max_completion_tokens(model)
        else _MAX_TOKENS_PARAM
    )
    return {param: token_limit}


def _error_body(error: BadRequestError) -> Dict[str, Any]:
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return {}
    nested_error = body.get("error")
    if isinstance(nested_error, dict):
        return nested_error
    return body


def _is_unsupported_param_error(error: BadRequestError, param: str) -> bool:
    body = _error_body(error)
    message = str(body.get("message") or error)
    normalized_message = message.lower()
    reported_param = body.get("param")
    code = body.get("code")
    param_matches = reported_param == param or param in message
    unsupported = (
        code == "unsupported_parameter"
        or "unsupported" in normalized_message
        or "not supported" in normalized_message
    )
    return param_matches and unsupported


def _alternate_token_limit_param(param: str) -> str:
    if param == _MAX_TOKENS_PARAM:
        return _MAX_COMPLETION_TOKENS_PARAM
    return _MAX_TOKENS_PARAM


async def create_chat_completion_with_token_limit(
    completions: Any,
    *,
    model: str,
    messages: Any,
    max_output_tokens: Optional[int] = None,
    **kwargs: Any,
) -> Any:
    """Create a chat completion and retry once with the alternate token limit param.

    This keeps model selection configurable: we use a model-family default for the
    first request, then handle OpenAI-compatible providers or newly changed model
    behavior by retrying only the known unsupported-parameter failure.
    """
    token_kwargs = chat_completion_token_limit_kwargs(model, max_output_tokens)
    request_kwargs = {**kwargs, **token_kwargs}
    try:
        return await completions.create(
            model=model,
            messages=messages,
            **request_kwargs,
        )
    except BadRequestError as error:
        if not token_kwargs:
            raise
        token_param, token_limit = next(iter(token_kwargs.items()))
        if not _is_unsupported_param_error(error, token_param):
            raise

        alternate_param = _alternate_token_limit_param(token_param)
        retry_kwargs = dict(kwargs)
        retry_kwargs[alternate_param] = token_limit
        return await completions.create(
            model=model,
            messages=messages,
            **retry_kwargs,
        )
