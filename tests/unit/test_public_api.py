def test_core_public_api_exports_pipeline_contract():
    from localize.core import (
        FileReporterConnector,
        FilesystemSourceConnector,
        FunctionProcessorConnector,
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
    assert FilesystemSourceConnector.__name__ == "FilesystemSourceConnector"
    assert FunctionProcessorConnector.__name__ == "FunctionProcessorConnector"
    assert FileReporterConnector.__name__ == "FileReporterConnector"


def test_provider_public_api_exports_default_backends():
    from localize.providers import (
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
    from localize.formats import (
        JSON_FORMAT,
        JAVA_PROPERTIES_FORMAT,
        LOCALE_DIRECTORY_LAYOUT,
        SUFFIX_LAYOUT,
        LocalizationFormat,
        LocalizationLayout,
        LocalizationProfile,
        get_localization_adapter,
        load_localization_format,
        load_localization_layout,
        load_localization_profiles,
    )

    assert JAVA_PROPERTIES_FORMAT.id == "java_properties"
    assert JSON_FORMAT.id == "json"
    assert SUFFIX_LAYOUT.id == "suffix"
    assert LOCALE_DIRECTORY_LAYOUT.id == "locale_directory"
    assert LocalizationFormat.__name__ == "LocalizationFormat"
    assert LocalizationLayout.__name__ == "LocalizationLayout"
    assert LocalizationProfile.__name__ == "LocalizationProfile"
    assert load_localization_format("java_properties") == JAVA_PROPERTIES_FORMAT
    assert load_localization_layout("suffix") == SUFFIX_LAYOUT
    assert load_localization_profiles({"localization_format": "json"})[0].localization_format == JSON_FORMAT
    assert get_localization_adapter(JSON_FORMAT).localization_format == JSON_FORMAT
