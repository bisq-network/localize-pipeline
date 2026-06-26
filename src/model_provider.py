"""Model-provider abstraction for chat completions, usage, tokens, and cost."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Mapping
from typing import Any, Dict, Optional, Protocol, Sequence

import tiktoken
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from src.cost_estimator import CostEstimate, estimate_run_cost, format_estimate
from src.openai_compat import create_chat_completion, create_chat_completion_with_fallback
from src.usage_tracker import DEFAULT_PRICES, UsageTracker


_LOCAL_PROVIDER_PLACEHOLDER_KEY = "not-needed"
DEFAULT_MODEL_PROVIDER = "aisuite"
DEFAULT_AISUITE_PROVIDER = "openai"
_AISUITE_OPENAI_ONLY_REQUEST_KWARGS = frozenset({
    "response_format",
})
_MODEL_PROVIDER_ALIASES = {
    "aisuite": "aisuite",
    "openai": "openai_compatible",
    "openai_compatible": "openai_compatible",
}


class ModelProviderConfigurationError(RuntimeError):
    """Raised when a model provider cannot be configured safely."""


class ChatModelProvider(Protocol):
    """Provider boundary used by the translation pipeline."""

    client: Any

    async def create_chat_completion(
        self,
        *,
        model: str,
        messages: Any,
        completion_token_limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """Create one chat completion and record usage when available."""

    def count_tokens(self, text: str, model_name: str) -> int:
        """Count tokens for provider/model context budgeting."""

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
        """Estimate run cost using provider-specific prices."""

    def format_estimate(self, estimate: CostEstimate) -> str:
        """Format a provider cost estimate for logging."""

    def record_response(self, model: str, response: Any) -> None:
        """Record token usage from a provider response."""

    def write_usage_summary(self, path: str) -> None:
        """Persist the accumulated provider usage summary."""

    def format_usage_summary(self) -> str:
        """Format the accumulated provider usage summary."""

    def is_retryable_error(self, exc: Exception) -> bool:
        """Return true when the provider error should follow retry backoff."""


def normalize_model_provider_name(provider_name: str) -> str:
    """Return the canonical provider name for config aliases."""
    normalized = (provider_name or DEFAULT_MODEL_PROVIDER).strip().lower().replace("-", "_")
    return _MODEL_PROVIDER_ALIASES.get(normalized, normalized)


def _aisuite_provider_key_for_model(model: str, default_provider: str) -> str:
    if ":" in model:
        return model.split(":", 1)[0].strip().lower()
    return default_provider.strip().lower()


def _aisuite_models_route_to_provider(
    model_names: Sequence[str],
    *,
    provider_key: str,
    default_provider: str,
) -> bool:
    models_to_check = tuple(model_names) or ("",)
    provider = provider_key.strip().lower()
    return any(
        _aisuite_provider_key_for_model(model_name, default_provider) == provider
        for model_name in models_to_check
    )


def _provider_config_has_credentials(provider_config: Mapping[str, Any]) -> bool:
    return any(provider_config.get(key) for key in ("api_key", "base_url"))


def _aisuite_provider_config(
    provider_configs: Mapping[str, Any],
    provider_key: str,
) -> Dict[str, Any]:
    if provider_key not in provider_configs:
        return {}

    provider_config = provider_configs[provider_key]
    if not isinstance(provider_config, Mapping):
        raise ModelProviderConfigurationError(
            f"aisuite.provider_configs.{provider_key} must be a mapping when configured."
        )
    return dict(provider_config)


def _sanitize_aisuite_request_kwargs(model: str, kwargs: Dict[str, Any], default_provider: str) -> Dict[str, Any]:
    provider_key = _aisuite_provider_key_for_model(model, default_provider)
    if provider_key == "openai":
        return dict(kwargs)
    return {
        key: value
        for key, value in kwargs.items()
        if key not in _AISUITE_OPENAI_ONLY_REQUEST_KWARGS
    }


class OpenAICompatibleProvider:
    """OpenAI-compatible chat-completion provider.

    This keeps OpenAI SDK details, token-limit parameter compatibility, tiktoken
    counting, pricing, and usage tracking behind one boundary.
    """

    def __init__(
        self,
        *,
        client: Any,
        usage_tracker: Optional[UsageTracker] = None,
        prices: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        self.client = client
        self._prices = prices if prices is not None else DEFAULT_PRICES
        self._usage_tracker = usage_tracker or UsageTracker(prices=self._prices)

    async def create_chat_completion(
        self,
        *,
        model: str,
        messages: Any,
        completion_token_limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        if self.client is None:
            raise ModelProviderConfigurationError("OpenAI-compatible client is not configured.")

        response = await create_chat_completion(
            self.client,
            model=model,
            messages=messages,
            completion_token_limit=completion_token_limit,
            **kwargs,
        )
        self.record_response(model, response)
        return response

    def count_tokens(self, text: str, model_name: str) -> int:
        """Count tokens with a provider-local fallback chain."""
        tokenizer_model_name = model_name.split(":", 1)[1] if ":" in model_name else model_name
        try:
            encoding = tiktoken.encoding_for_model(tokenizer_model_name)
        except Exception:
            try:
                encoding = tiktoken.get_encoding("gpt2")
            except Exception:
                return len(text.split())

        try:
            return len(encoding.encode(text))
        except Exception:
            return len(text.split())

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
            prices=self._prices,
        )

    def format_estimate(self, estimate: CostEstimate) -> str:
        return format_estimate(estimate)

    def record_response(self, model: str, response: Any) -> None:
        self._usage_tracker.record_response(model, response)

    def write_usage_summary(self, path: str) -> None:
        self._usage_tracker.write_json(path)

    def format_usage_summary(self) -> str:
        return self._usage_tracker.format_summary()

    def is_retryable_error(self, exc: Exception) -> bool:
        if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)):
            return True
        if isinstance(exc, APIStatusError):
            return getattr(exc, "status_code", 0) >= 500
        return False


class AiSuiteProvider(OpenAICompatibleProvider):
    """AISuite-backed provider using the same async boundary as the pipeline.

    AISuite exposes a synchronous OpenAI-style client. This adapter runs the
    blocking call in a worker thread and keeps usage/cost/token APIs identical
    to the direct OpenAI-compatible provider.
    """

    def __init__(
        self,
        *,
        client: Any,
        usage_tracker: Optional[UsageTracker] = None,
        prices: Optional[Dict[str, Dict[str, float]]] = None,
        default_provider: str = DEFAULT_AISUITE_PROVIDER,
    ) -> None:
        super().__init__(client=client, usage_tracker=usage_tracker, prices=prices)
        self.default_provider = default_provider

    def _provider_model_name(self, model: str) -> str:
        if ":" in model:
            return model
        return f"{self.default_provider}:{model}"

    async def create_chat_completion(
        self,
        *,
        model: str,
        messages: Any,
        completion_token_limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        if self.client is None:
            raise ModelProviderConfigurationError("AISuite client is not configured.")

        provider_model_name = self._provider_model_name(model)
        request_kwargs = _sanitize_aisuite_request_kwargs(
            provider_model_name,
            kwargs,
            self.default_provider,
        )

        async def call_aisuite(**request_kwargs: Any) -> Any:
            return await asyncio.to_thread(
                self.client.chat.completions.create,
                **request_kwargs,
            )

        response = await create_chat_completion_with_fallback(
            call_aisuite,
            model=provider_model_name,
            messages=messages,
            completion_token_limit=completion_token_limit,
            retry_exception_types=(Exception,),
            **request_kwargs,
        )
        self.record_response(model, response)
        return response

    def is_retryable_error(self, exc: Exception) -> bool:
        if super().is_retryable_error(exc):
            return True
        exc_module = exc.__class__.__module__
        exc_name = exc.__class__.__name__
        return exc_module.startswith("aisuite") or exc_name == "LLMError"


def create_openai_compatible_provider(
    *,
    api_key: Optional[str],
    api_base_url: Optional[str],
    logger: logging.Logger,
) -> OpenAICompatibleProvider:
    """Create an OpenAI-compatible provider from resolved credentials."""
    is_custom_endpoint = bool(api_base_url)
    resolved_key = api_key

    if not resolved_key:
        if is_custom_endpoint:
            logger.info(
                "No OPENAI_API_KEY set; using a placeholder key for custom endpoint '%s' "
                "(typical for local servers such as Ollama).",
                api_base_url,
            )
            resolved_key = _LOCAL_PROVIDER_PLACEHOLDER_KEY
        else:
            raise ModelProviderConfigurationError(
                "OPENAI_API_KEY is required when no custom api_base_url is configured."
            )
    elif not is_custom_endpoint and not resolved_key.startswith("sk-"):
        logger.warning("Warning: OPENAI_API_KEY does not start with 'sk-'. This may be invalid.")

    client_kwargs: Dict[str, Any] = {"api_key": resolved_key}
    if api_base_url:
        client_kwargs["base_url"] = api_base_url
        logger.info("Using OpenAI-compatible endpoint: %s", api_base_url)

    client = AsyncOpenAI(**client_kwargs)
    logger.info("OpenAI-compatible model provider initialized successfully")
    return OpenAICompatibleProvider(client=client)


def create_aisuite_provider(
    *,
    provider_configs: Dict[str, Any],
    logger: logging.Logger,
    default_provider: str = DEFAULT_AISUITE_PROVIDER,
) -> AiSuiteProvider:
    """Create the AISuite-backed provider used by the default runtime path."""
    try:
        aisuite = importlib.import_module("aisuite")
    except ImportError as exc:
        raise ModelProviderConfigurationError(
            "AISuite provider selected, but the 'aisuite' package is not installed."
        ) from exc

    client = aisuite.Client(provider_configs=provider_configs)
    logger.info("AISuite model provider initialized successfully")
    return AiSuiteProvider(client=client, default_provider=default_provider)


def _openai_provider_config(
    *,
    api_key: Optional[str],
    api_base_url: Optional[str],
) -> Dict[str, str]:
    config: Dict[str, str] = {}
    if api_key:
        config["api_key"] = api_key
    elif api_base_url:
        config["api_key"] = _LOCAL_PROVIDER_PLACEHOLDER_KEY
    if api_base_url:
        config["base_url"] = api_base_url
    return config


def create_model_provider(
    *,
    provider_name: str,
    api_key: Optional[str],
    api_base_url: Optional[str],
    logger: logging.Logger,
    aisuite_provider_configs: Optional[Dict[str, Any]] = None,
    model_names: Sequence[str] = (),
) -> ChatModelProvider:
    """Create the configured model provider backend."""
    normalized = normalize_model_provider_name(provider_name)
    if normalized == "openai_compatible":
        return create_openai_compatible_provider(
            api_key=api_key,
            api_base_url=api_base_url,
            logger=logger,
        )
    if normalized == "aisuite":
        provider_configs: Dict[str, Any] = dict(aisuite_provider_configs or {})
        openai_provider_config = _aisuite_provider_config(
            provider_configs,
            DEFAULT_AISUITE_PROVIDER,
        )
        openai_config = _openai_provider_config(
            api_key=api_key,
            api_base_url=api_base_url,
        )
        if openai_config:
            provider_configs["openai"] = {
                **openai_provider_config,
                **openai_config,
            }
            openai_provider_config = provider_configs["openai"]
        if (
            _aisuite_models_route_to_provider(
                model_names,
                provider_key=DEFAULT_AISUITE_PROVIDER,
                default_provider=DEFAULT_AISUITE_PROVIDER,
            )
            and not _provider_config_has_credentials(openai_provider_config)
        ):
            raise ModelProviderConfigurationError(
                "OPENAI_API_KEY is required when AISuite routes bare or openai: models "
                "to the default OpenAI API without a custom api_base_url."
            )
        return create_aisuite_provider(
            provider_configs=provider_configs,
            logger=logger,
            default_provider=DEFAULT_AISUITE_PROVIDER,
        )
    raise ModelProviderConfigurationError(
        f"Unsupported model_provider '{provider_name}'. Supported values: openai_compatible, aisuite."
    )
