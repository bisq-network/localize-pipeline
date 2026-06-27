from localize.model_provider import (
    AiSuiteProvider,
    ModelProviderCapabilities,
    OpenAICompatibleProvider,
)


def test_openai_compatible_provider_reports_structured_output_capability():
    provider = OpenAICompatibleProvider(client=None)

    capabilities = provider.capabilities_for_model("gpt-4o")

    assert capabilities == ModelProviderCapabilities(
        provider_key="openai_compatible",
        supports_response_format=True,
        supports_completion_token_limit=True,
    )


def test_aisuite_reports_openai_capabilities_for_bare_default_models():
    provider = AiSuiteProvider(client=None, default_provider="openai")

    capabilities = provider.capabilities_for_model("gpt-4o")

    assert capabilities.provider_key == "openai"
    assert capabilities.supports_response_format is True
    assert capabilities.supports_completion_token_limit is True


def test_aisuite_reports_reduced_capabilities_for_non_openai_models():
    provider = AiSuiteProvider(client=None, default_provider="openai")

    capabilities = provider.capabilities_for_model("anthropic:claude-3-5-sonnet-latest")

    assert capabilities.provider_key == "anthropic"
    assert capabilities.supports_response_format is False
    assert capabilities.supports_completion_token_limit is True
