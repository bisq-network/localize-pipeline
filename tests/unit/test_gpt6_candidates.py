"""Candidate model contracts, without paid inference or private configuration."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiolimiter import AsyncLimiter

from localize.app_config import load_app_config, validate_config
from localize.json_response import chat_reasoning_effort_kwargs
from localize.providers.models import (
    AiSuiteProvider,
    ModelProviderConfigurationError,
    OpenAICompatibleProvider,
)
from localize.providers.usage import cost_for_tokens


CANDIDATES = (
    ("gpt-6-luna", "none", 0.10, 0.01, 0.125, 0.50),
    ("gpt-6-sol", "none", 2.00, 0.20, 2.50, 10.00),
    ("gpt-6.1-sol", "low", 2.00, 0.10, 2.50, 10.00),
)


@pytest.mark.parametrize("model,effort,input_rate,cached_rate,write_rate,output_rate", CANDIDATES)
@pytest.mark.parametrize("route", ["direct", "aisuite"])
@pytest.mark.asyncio
async def test_candidate_effort_reaches_actual_client(model, effort, input_rate, cached_rate, write_rate, output_rate, route):
    response = SimpleNamespace(usage=None, choices=[])
    create = AsyncMock(return_value=response) if route == "direct" else MagicMock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider_class = OpenAICompatibleProvider if route == "direct" else AiSuiteProvider
    provider = provider_class(client=client, supports_openai_reasoning_effort=True)
    routed_model = model if route == "direct" else f"openai:{model}"

    await provider.create_chat_completion(
        model=routed_model,
        messages=[],
        completion_token_limit=1234,
        temperature=0.3,
        response_format={"type": "json_object"},
        **chat_reasoning_effort_kwargs(provider, routed_model, effort),
    )

    request = create.call_args.kwargs
    assert request["reasoning_effort"] == effort
    assert request["max_completion_tokens"] == 1234
    assert "max_tokens" not in request
    assert "temperature" not in request
    assert request["response_format"] == {"type": "json_object"}
    assert create.call_count == 1


@pytest.mark.parametrize("route", ["direct", "aisuite"])
@pytest.mark.parametrize("model,effort", [("gpt-6.1-sol", "none"), ("gpt-6-luna", "minimal")])
@pytest.mark.asyncio
async def test_invalid_candidate_effort_never_silently_defaults(route, model, effort):
    create = AsyncMock() if route == "direct" else MagicMock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider_class = OpenAICompatibleProvider if route == "direct" else AiSuiteProvider
    provider = provider_class(client=client, supports_openai_reasoning_effort=True)

    with pytest.raises(ModelProviderConfigurationError, match="reasoning_effort.*not supported"):
        await provider.create_chat_completion(model=model, messages=[], reasoning_effort=effort)

    create.assert_not_called()


@pytest.mark.parametrize("model,effort,input_rate,cached_rate,write_rate,output_rate", CANDIDATES)
@pytest.mark.parametrize("prompt_tokens", [272_000, 272_001])
def test_candidate_pricing_partitions_cache_and_applies_context_threshold(
    model, effort, input_rate, cached_rate, write_rate, output_rate, prompt_tokens,
):
    input_multiplier = 2 if prompt_tokens > 272_000 else 1
    output_multiplier = 1.5 if prompt_tokens > 272_000 else 1
    expected = (
        ((prompt_tokens - 150_000) * input_rate + 100_000 * cached_rate + 50_000 * write_rate)
        * input_multiplier + 10_000 * output_rate * output_multiplier
    ) / 1_000_000
    assert cost_for_tokens(
        f"openai:{model}", prompt_tokens, 10_000,
        cached_tokens=100_000, cache_write_tokens=50_000,
    ) == pytest.approx(expected)


def _load_config(config, env=None):
    with (
        patch.dict(os.environ, env or {}, clear=True),
        patch("localize.app_config._load_dotenv_files"),
        patch("localize.app_config._load_yaml_config", return_value={"dry_run": True, **config}),
        patch("localize.app_config._setup_logger_from_config", return_value=MagicMock()),
    ):
        return load_app_config()


@pytest.mark.parametrize("config_effort,env,expected", [
    (None, {}, None),
    ("none", {}, "none"),
    ("none", {"MODEL_REASONING_EFFORT": "low"}, "low"),
])
def test_draft_effort_loading_and_env_precedence(config_effort, env, expected):
    config = _load_config({"model_name": "gpt-6-luna", "model_reasoning_effort": config_effort}, env)
    assert config.model_reasoning_effort == expected


@pytest.mark.parametrize("field", ["model_reasoning_effort", "review_reasoning_effort"])
@pytest.mark.parametrize("effort", ["none", "minimal", "medum"])
def test_invalid_sol61_effort_is_rejected_before_client_initialization(field, effort):
    model_field = "model_name" if field == "model_reasoning_effort" else "review_model_name"
    config = {model_field: "openai:gpt-6.1-sol", field: effort}
    issues = validate_config({"dry_run": True, **config})
    assert any(issue.level == "error" and field in issue.message for issue in issues)
    with pytest.raises(SystemExit):
        _load_config(config)


def test_semantic_sol61_effort_rejected():
    issues = validate_config({"dry_run": True, "semantic_review": {
        "enabled": True, "model": "gpt-6.1-sol", "reasoning_effort": "none",
    }})
    assert any("semantic_review.reasoning_effort" in issue.message for issue in issues)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "invalid"])
def test_draft_completion_cap_requires_positive_integer(limit):
    config = {"model_max_completion_tokens": limit}
    issues = validate_config({"dry_run": True, **config})
    assert any("model_max_completion_tokens" in issue.message for issue in issues)
    with pytest.raises(SystemExit):
        _load_config(config)


@pytest.mark.parametrize("limit", [None, 256])
def test_draft_completion_cap_loading(limit):
    config = _load_config({"model_max_completion_tokens": limit})
    assert config.model_max_completion_tokens == limit


@pytest.mark.parametrize("model,effort", [("gpt-6-luna", "none"), ("gpt-6-sol", "none"), ("gpt-6.1-sol", "low")])
def test_supported_gpt6_draft_efforts_load(model, effort):
    assert _load_config({"model_name": model, "model_reasoning_effort": effort}).model_reasoning_effort == effort


@pytest.mark.parametrize("effort", ["none", None])
@pytest.mark.parametrize("limit", [256, None])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
@pytest.mark.asyncio
async def test_draft_call_passes_only_explicit_configured_effort(monkeypatch, effort, limit, finish_reason):
    from localize import translate_localization_files as translator

    provider = OpenAICompatibleProvider(client=None, supports_openai_reasoning_effort=True)
    provider.create_chat_completion = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Speichern"), finish_reason=finish_reason)],
    ))
    monkeypatch.setattr(translator, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(translator, "MODEL_NAME", "gpt-6-luna")
    monkeypatch.setattr(translator, "MODEL_REASONING_EFFORT", effort, raising=False)
    monkeypatch.setattr(translator, "MODEL_MAX_COMPLETION_TOKENS", limit, raising=False)

    result = await translator.translate_text_async(
        "Save", "button.save", {}, {}, "German", {},
        asyncio.Semaphore(1), AsyncLimiter(1000), 0,
    )

    assert result == ((0, "Save", False) if finish_reason == "length" else (0, "Speichern", True))
    request = provider.create_chat_completion.await_args.kwargs
    if effort is None:
        assert "reasoning_effort" not in request
    else:
        assert request["reasoning_effort"] == effort
    if limit is None:
        assert "completion_token_limit" not in request
    else:
        assert request["completion_token_limit"] == limit
