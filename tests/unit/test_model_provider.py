import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import APIStatusError, OpenAIError

from localize.model_provider import (
    AiSuiteProvider,
    ModelProviderConfigurationError,
    OpenAICompatibleProvider,
    create_model_provider,
    create_aisuite_provider,
    create_openai_compatible_provider,
    normalize_model_provider_name,
    requires_openai_credentials,
)
from localize.usage_tracker import UsageTracker


def _response(content="{}", prompt_tokens=11, completion_tokens=7):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _api_status_error(status_code):
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://api.example.test/chat"),
    )
    return APIStatusError("request failed", response=response, body={})


@pytest.mark.asyncio
async def test_provider_delegates_chat_completion_and_records_usage():
    tracker = UsageTracker(prices={"custom-model": {"input": 1.0, "output": 2.0}})
    provider = OpenAICompatibleProvider(client=object(), usage_tracker=tracker)
    response = _response(prompt_tokens=123, completion_tokens=45)

    with patch(
        "localize.model_provider.create_chat_completion",
        AsyncMock(return_value=response),
    ) as create_completion:
        result = await provider.create_chat_completion(
            model="custom-model",
            messages=[{"role": "user", "content": "translate"}],
            completion_token_limit=512,
            temperature=0.1,
        )

    assert result is response
    create_completion.assert_awaited_once_with(
        provider.client,
        model="custom-model",
        messages=[{"role": "user", "content": "translate"}],
        completion_token_limit=512,
        temperature=0.1,
    )
    summary = tracker.summary()
    assert summary["models"]["custom-model"]["prompt_tokens"] == 123
    assert summary["models"]["custom-model"]["completion_tokens"] == 45


def test_provider_count_tokens_uses_tiktoken_fallback():
    provider = OpenAICompatibleProvider(client=None)
    fake_encoding = MagicMock()
    fake_encoding.encode.side_effect = lambda text: text.split()

    with (
        patch("localize.model_provider.tiktoken.encoding_for_model", side_effect=Exception),
        patch("localize.model_provider.tiktoken.get_encoding", return_value=fake_encoding),
    ):
        assert provider.count_tokens("one two three", "unknown-model") == 3


def test_provider_count_tokens_strips_provider_prefix_for_tiktoken():
    provider = OpenAICompatibleProvider(client=None)
    fake_encoding = MagicMock()
    fake_encoding.encode.return_value = [1, 2, 3]

    with patch(
        "localize.model_provider.tiktoken.encoding_for_model",
        return_value=fake_encoding,
    ) as encoding_for_model:
        assert provider.count_tokens("one two three", "openai:gpt-4o") == 3

    encoding_for_model.assert_called_once_with("gpt-4o")


def test_provider_cost_estimate_uses_provider_price_table():
    provider = OpenAICompatibleProvider(
        client=None,
        prices={"local-model": {"input": 0.0, "output": 0.0}},
    )

    estimate = provider.estimate_run_cost(
        num_keys=10,
        locale_codes=["de", "es"],
        translate_model="local-model",
        review_model="local-model",
    )

    assert estimate.cost_complete is True
    assert estimate.estimated_cost_usd == 0.0


def test_provider_cost_estimate_handles_aisuite_openai_model_prefixes():
    provider = OpenAICompatibleProvider(client=None)

    estimate = provider.estimate_run_cost(
        num_keys=10,
        locale_codes=["de"],
        translate_model="openai:gpt-4o-mini",
        review_model="openai:gpt-4o",
    )

    assert estimate.cost_complete is True
    assert estimate.estimated_cost_usd is not None


def test_provider_usage_summary_is_written_by_provider(tmp_path):
    provider = OpenAICompatibleProvider(client=None)
    provider.record_response("gpt-4o-mini", _response(prompt_tokens=9, completion_tokens=3))

    path = tmp_path / "usage" / "summary.json"
    provider.write_usage_summary(str(path))

    assert path.exists()
    assert "gpt-4o-mini" in provider.format_usage_summary()


def test_factory_builds_default_openai_client():
    logger = logging.getLogger("test")

    with patch("localize.model_provider.AsyncOpenAI", return_value=object()) as openai:
        provider = create_openai_compatible_provider(
            api_key="sk-test",
            api_base_url=None,
            logger=logger,
        )

    openai.assert_called_once_with(api_key="sk-test")
    assert isinstance(provider, OpenAICompatibleProvider)


def test_factory_builds_custom_endpoint_with_placeholder_key():
    logger = logging.getLogger("test")

    with patch("localize.model_provider.AsyncOpenAI", return_value=object()) as openai:
        provider = create_openai_compatible_provider(
            api_key=None,
            api_base_url="http://localhost:11434/v1",
            logger=logger,
        )

    _, kwargs = openai.call_args
    assert kwargs["api_key"]
    assert kwargs["base_url"] == "http://localhost:11434/v1"
    assert isinstance(provider, OpenAICompatibleProvider)


def test_normalize_model_provider_name_canonicalizes_aliases():
    assert normalize_model_provider_name("") == "aisuite"
    assert normalize_model_provider_name("openai") == "openai_compatible"
    assert normalize_model_provider_name("openai-compatible") == "openai_compatible"
    assert normalize_model_provider_name("AISUITE") == "aisuite"


def test_openai_provider_retries_only_transient_errors():
    provider = OpenAICompatibleProvider(client=None)

    assert provider.is_retryable_error(_api_status_error(500)) is True
    assert provider.is_retryable_error(_api_status_error(503)) is True
    assert provider.is_retryable_error(_api_status_error(400)) is False
    assert provider.is_retryable_error(_api_status_error(401)) is False
    assert provider.is_retryable_error(OpenAIError("permanent")) is False


class _FakeSyncCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeSyncClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FakeSyncCompletions(response))

    @property
    def calls(self):
        return self.chat.completions.calls


@pytest.mark.asyncio
async def test_aisuite_provider_uses_sync_client_behind_async_boundary():
    tracker = UsageTracker(prices={"gpt-4o": {"input": 1.0, "output": 2.0}})
    client = _FakeSyncClient(_response(prompt_tokens=3, completion_tokens=2))
    provider = AiSuiteProvider(client=client, usage_tracker=tracker)

    result = await provider.create_chat_completion(
        model="openai:gpt-4o",
        messages=[{"role": "user", "content": "translate"}],
        completion_token_limit=128,
        temperature=0,
    )

    assert result.usage.prompt_tokens == 3
    assert client.calls[0]["model"] == "openai:gpt-4o"
    assert client.calls[0]["max_tokens"] == 128
    assert tracker.summary()["models"]["openai:gpt-4o"]["total_tokens"] == 5


@pytest.mark.asyncio
async def test_aisuite_provider_defaults_bare_model_names_to_openai():
    client = _FakeSyncClient(_response())
    provider = AiSuiteProvider(client=client, default_provider="openai")

    await provider.create_chat_completion(
        model="gpt-4o",
        messages=[{"role": "user", "content": "translate"}],
    )

    assert client.calls[0]["model"] == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_aisuite_provider_keeps_openai_only_kwargs_for_openai_models():
    client = _FakeSyncClient(_response())
    provider = AiSuiteProvider(client=client, default_provider="openai")

    await provider.create_chat_completion(
        model="gpt-4o",
        messages=[{"role": "system", "content": "return json"}],
        response_format={"type": "json_object"},
        completion_token_limit=128,
    )

    assert client.calls[0]["model"] == "openai:gpt-4o"
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert client.calls[0]["max_tokens"] == 128


@pytest.mark.asyncio
async def test_aisuite_provider_strips_openai_only_kwargs_for_non_openai_models():
    client = _FakeSyncClient(_response())
    provider = AiSuiteProvider(client=client, default_provider="openai")

    await provider.create_chat_completion(
        model="anthropic:claude-3-5-sonnet-latest",
        messages=[{"role": "system", "content": "return json"}],
        response_format={"type": "json_object"},
        completion_token_limit=128,
        temperature=0.1,
    )

    assert client.calls[0]["model"] == "anthropic:claude-3-5-sonnet-latest"
    assert "response_format" not in client.calls[0]
    assert client.calls[0]["temperature"] == 0.1
    assert client.calls[0]["max_tokens"] == 128


def test_aisuite_factory_imports_aisuite_lazily():
    logger = logging.getLogger("test")
    fake_client = object()

    with patch("localize.model_provider.importlib.import_module") as import_module:
        import_module.return_value = SimpleNamespace(
            Client=MagicMock(return_value=fake_client)
        )
        provider = create_aisuite_provider(
            provider_configs={"openai": {"api_key": "sk-test"}},
            logger=logger,
        )

    import_module.assert_called_once_with("aisuite")
    import_module.return_value.Client.assert_called_once_with(
        provider_configs={"openai": {"api_key": "sk-test"}}
    )
    assert isinstance(provider, AiSuiteProvider)
    assert provider.client is fake_client


def test_provider_factory_can_select_aisuite_backend():
    logger = logging.getLogger("test")

    with patch("localize.model_provider.create_aisuite_provider") as create_aisuite:
        create_aisuite.return_value = object()
        provider = create_model_provider(
            provider_name="aisuite",
            api_key="sk-test",
            api_base_url=None,
            logger=logger,
            aisuite_provider_configs={},
        )

    assert provider is create_aisuite.return_value
    create_aisuite.assert_called_once()


def test_provider_factory_defaults_to_aisuite_backend():
    logger = logging.getLogger("test")

    with patch("localize.model_provider.create_aisuite_provider") as create_aisuite:
        create_aisuite.return_value = object()
        provider = create_model_provider(
            provider_name="",
            api_key="sk-test",
            api_base_url=None,
            logger=logger,
            aisuite_provider_configs={},
        )

    assert provider is create_aisuite.return_value


def test_aisuite_factory_adds_openai_placeholder_for_custom_endpoint_without_key():
    logger = logging.getLogger("test")

    with patch("localize.model_provider.create_aisuite_provider") as create_aisuite:
        create_aisuite.return_value = object()
        create_model_provider(
            provider_name="aisuite",
            api_key=None,
            api_base_url="http://localhost:11434/v1",
            logger=logger,
            aisuite_provider_configs={},
        )

    _, kwargs = create_aisuite.call_args
    assert kwargs["provider_configs"]["openai"]["base_url"] == "http://localhost:11434/v1"
    assert kwargs["provider_configs"]["openai"]["api_key"]


def test_aisuite_factory_requires_openai_credentials_for_default_models():
    logger = logging.getLogger("test")

    with pytest.raises(ModelProviderConfigurationError, match="OPENAI_API_KEY"):
        create_model_provider(
            provider_name="aisuite",
            api_key=None,
            api_base_url=None,
            logger=logger,
            aisuite_provider_configs={},
            model_names=("gpt-4o-mini", "gpt-4o"),
        )


def test_openai_credential_requirement_matches_aisuite_model_routes():
    assert requires_openai_credentials(
        provider_name="aisuite",
        model_names=("gpt-4o-mini", "gpt-4o"),
        api_base_url=None,
        aisuite_provider_configs={},
    )
    assert not requires_openai_credentials(
        provider_name="aisuite",
        model_names=("anthropic:claude-3-5-sonnet-latest",),
        api_base_url=None,
        aisuite_provider_configs={"anthropic": {"api_key": "secret"}},
    )
    assert not requires_openai_credentials(
        provider_name="aisuite",
        model_names=("gpt-4o-mini",),
        api_base_url="http://localhost:11434/v1",
        aisuite_provider_configs={},
    )
    assert requires_openai_credentials(
        provider_name="openai_compatible",
        model_names=("any-model",),
        api_base_url=None,
        aisuite_provider_configs={},
    )


def test_openai_credential_requirement_validates_aisuite_openai_config_with_base_url():
    with pytest.raises(ModelProviderConfigurationError, match="aisuite.provider_configs.openai"):
        requires_openai_credentials(
            provider_name="aisuite",
            model_names=("gpt-4o-mini",),
            api_base_url="http://localhost:11434/v1",
            aisuite_provider_configs={"openai": ["api_key", "secret"]},
        )


def test_aisuite_factory_allows_non_openai_models_without_openai_key():
    logger = logging.getLogger("test")
    provider_configs = {"anthropic": {"api_key": "secret-from-provider-config"}}

    with patch("localize.model_provider.create_aisuite_provider") as create_aisuite:
        create_aisuite.return_value = object()
        provider = create_model_provider(
            provider_name="aisuite",
            api_key=None,
            api_base_url=None,
            logger=logger,
            aisuite_provider_configs=provider_configs,
            model_names=("anthropic:claude-3-5-sonnet-latest",),
        )

    assert provider is create_aisuite.return_value
    _, kwargs = create_aisuite.call_args
    assert kwargs["provider_configs"] == provider_configs


def test_aisuite_factory_requires_openai_credentials_for_mixed_model_routes():
    logger = logging.getLogger("test")

    with pytest.raises(ModelProviderConfigurationError, match="OPENAI_API_KEY"):
        create_model_provider(
            provider_name="aisuite",
            api_key=None,
            api_base_url=None,
            logger=logger,
            aisuite_provider_configs={"anthropic": {"api_key": "secret"}},
            model_names=("anthropic:claude-3-5-sonnet-latest", "openai:gpt-4o"),
        )


@pytest.mark.parametrize("openai_config", [None, "invalid", ["api_key", "secret"]])
def test_aisuite_factory_rejects_invalid_openai_provider_config(openai_config):
    logger = logging.getLogger("test")

    with pytest.raises(
        ModelProviderConfigurationError,
        match="aisuite.provider_configs.openai",
    ):
        create_model_provider(
            provider_name="aisuite",
            api_key=None,
            api_base_url=None,
            logger=logger,
            aisuite_provider_configs={"openai": openai_config},
            model_names=("gpt-4o-mini",),
        )


def test_aisuite_factory_rejects_invalid_openai_config_before_endpoint_merge():
    logger = logging.getLogger("test")

    with pytest.raises(
        ModelProviderConfigurationError,
        match="aisuite.provider_configs.openai",
    ):
        create_model_provider(
            provider_name="aisuite",
            api_key=None,
            api_base_url="http://localhost:11434/v1",
            logger=logger,
            aisuite_provider_configs={"openai": None},
            model_names=("gpt-4o-mini",),
        )
