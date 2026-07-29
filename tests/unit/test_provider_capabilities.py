from localize.model_provider import (
    AiSuiteProvider,
    ModelProviderCapabilities,
    OpenAICompatibleProvider,
)
from localize.json_response import chat_reasoning_effort_kwargs


def test_openai_compatible_provider_reports_structured_output_capability():
    provider = OpenAICompatibleProvider(client=None)

    capabilities = provider.capabilities_for_model("gpt-4o")

    assert capabilities == ModelProviderCapabilities(
        provider_key="openai_compatible",
        supports_response_format=True,
        supports_completion_token_limit=True,
        supports_reasoning_effort=False,
    )


def test_official_openai_provider_reports_reasoning_capability_for_gpt5():
    provider = OpenAICompatibleProvider(
        client=None,
        supports_openai_reasoning_effort=True,
    )

    assert provider.capabilities_for_model(
        "gpt-5.6-terra"
    ).supports_reasoning_effort is True
    assert provider.capabilities_for_model("gpt-4o").supports_reasoning_effort is False


def test_aisuite_reports_model_specific_openai_capabilities():
    provider = AiSuiteProvider(
        client=None,
        default_provider="openai",
        supports_openai_reasoning_effort=True,
    )

    capabilities = provider.capabilities_for_model("gpt-5.6-terra")

    assert capabilities.provider_key == "openai"
    assert capabilities.supports_response_format is True
    assert capabilities.supports_completion_token_limit is True
    assert capabilities.supports_reasoning_effort is True


def test_aisuite_reports_reduced_capabilities_for_non_openai_models():
    provider = AiSuiteProvider(client=None, default_provider="openai")

    capabilities = provider.capabilities_for_model("anthropic:claude-3-5-sonnet-latest")

    assert capabilities.provider_key == "anthropic"
    assert capabilities.supports_response_format is False
    assert capabilities.supports_completion_token_limit is True
    assert capabilities.supports_reasoning_effort is False


def test_reasoning_effort_is_only_sent_to_supported_routes():
    openai_provider = AiSuiteProvider(
        client=None,
        default_provider="openai",
        supports_openai_reasoning_effort=True,
    )

    assert chat_reasoning_effort_kwargs(
        openai_provider,
        "gpt-5.6-terra",
        "none",
    ) == {"reasoning_effort": "none"}
    assert chat_reasoning_effort_kwargs(
        openai_provider,
        "anthropic:claude-3-5-sonnet-latest",
        "none",
    ) == {}
    assert chat_reasoning_effort_kwargs(
        openai_provider,
        "gpt-4o",
        "none",
    ) == {}
    assert chat_reasoning_effort_kwargs(
        openai_provider,
        "gpt-5.6-terra",
        None,
    ) == {}
