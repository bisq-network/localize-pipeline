def test_core_public_api_exports_pipeline_contract():
    from src.core import (
        TranslationPipelineOptions,
        TranslationPipelinePaths,
        TranslationPipelineResult,
        TranslationPipelineSteps,
        run_translation_pipeline,
    )

    assert callable(run_translation_pipeline)
    assert TranslationPipelinePaths.__name__ == "TranslationPipelinePaths"
    assert TranslationPipelineOptions.__name__ == "TranslationPipelineOptions"
    assert TranslationPipelineSteps.__name__ == "TranslationPipelineSteps"
    assert TranslationPipelineResult.__name__ == "TranslationPipelineResult"


def test_provider_public_api_exports_default_backends():
    from src.providers import (
        AiSuiteProvider,
        DEFAULT_AISUITE_PROVIDER,
        DEFAULT_MODEL_PROVIDER,
        ChatModelProvider,
        OpenAICompatibleProvider,
        create_model_provider,
        normalize_model_provider_name,
    )

    assert AiSuiteProvider.__name__ == "AiSuiteProvider"
    assert OpenAICompatibleProvider.__name__ == "OpenAICompatibleProvider"
    assert ChatModelProvider.__name__ == "ChatModelProvider"
    assert DEFAULT_MODEL_PROVIDER == "aisuite"
    assert DEFAULT_AISUITE_PROVIDER == "openai"
    assert callable(create_model_provider)
    assert normalize_model_provider_name("openai") == "openai_compatible"


def test_format_public_api_exports_localization_format_metadata():
    from src.formats import JAVA_PROPERTIES_FORMAT, LocalizationFormat, load_localization_format

    assert JAVA_PROPERTIES_FORMAT.id == "java_properties"
    assert LocalizationFormat.__name__ == "LocalizationFormat"
    assert load_localization_format("java_properties") == JAVA_PROPERTIES_FORMAT
