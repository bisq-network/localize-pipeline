"""Unit tests for localization format metadata and filename helpers."""

import pytest

from src.localization_formats import (
    JSON_FORMAT,
    JAVA_PROPERTIES_FORMAT,
    LocalizationFormat,
    load_localization_format,
)


def test_java_properties_format_matches_existing_filename_conventions():
    fmt = JAVA_PROPERTIES_FORMAT

    assert fmt.id == "java_properties"
    assert fmt.file_extension == ".properties"
    assert fmt.code_fence == "properties"
    assert fmt.is_locale_file("messages_de.properties")
    assert fmt.extract_locale_suffix("messages_pt_BR.properties") == "pt_BR"
    assert fmt.extract_locale_suffix("mu_sig_de.properties") == "de"
    assert fmt.extract_supported_locale_suffix("mu_sig_de.properties", ["de", "sig"]) == "de"
    assert fmt.extract_locale_suffix("messages.properties") is None
    assert fmt.source_filename("bisq_easy_pt_BR.properties", ["pt_BR", "pt"]) == "bisq_easy.properties"
    assert fmt.source_filename("mu_sig_de.properties", ["de", "sig"]) == "mu_sig.properties"


def test_java_properties_format_exposes_cached_compiled_regex():
    assert JAVA_PROPERTIES_FORMAT.compiled_locale_suffix_regex.search("messages_de.properties")
    assert (
        JAVA_PROPERTIES_FORMAT.compiled_locale_suffix_regex
        is JAVA_PROPERTIES_FORMAT.compiled_locale_suffix_regex
    )


def test_custom_format_can_describe_future_locale_file_conventions():
    fmt = LocalizationFormat(
        id="json_flat",
        display_name="Flat JSON",
        file_extension=".json",
        code_fence="json",
        locale_suffix_regex=r"_([a-z]{2}(?:-[A-Z]{2})?)\.json$",
    )

    assert fmt.is_supported_file("messages_de.json")
    assert fmt.is_locale_file("messages_pt-BR.json")
    assert fmt.extract_locale_suffix("messages_pt-BR.json") == "pt-BR"
    assert fmt.source_filename("messages_pt-BR.json", ["pt-BR", "pt"]) == "messages.json"


def test_load_localization_format_defaults_to_java_properties():
    assert load_localization_format(None) == JAVA_PROPERTIES_FORMAT
    assert load_localization_format("java_properties") == JAVA_PROPERTIES_FORMAT
    assert load_localization_format("json") == JSON_FORMAT


def test_load_localization_format_allows_overrides_for_future_formats():
    fmt = load_localization_format({
        "id": "custom_json",
        "display_name": "Custom JSON",
        "file_extension": ".json",
        "code_fence": "json",
        "locale_suffix_regex": r"\.([a-z]{2})\.json$",
    })

    assert fmt.id == "custom_json"
    assert fmt.extract_locale_suffix("messages.de.json") == "de"
    assert fmt.source_filename("messages.de.json", ["de"]) == "messages.json"


def test_load_localization_format_rejects_unknown_registry_id():
    with pytest.raises(ValueError, match="Unsupported localization_format"):
        load_localization_format("android_xml")


def test_json_format_matches_common_locale_filename_conventions():
    fmt = JSON_FORMAT

    assert fmt.id == "json"
    assert fmt.file_extension == ".json"
    assert fmt.code_fence == "json"
    assert fmt.is_locale_file("messages_de.json")
    assert fmt.is_locale_file("messages.de.json")
    assert fmt.is_locale_file("messages-de.json")
    assert fmt.extract_supported_locale_suffix("messages_pt_BR.json", ["pt_BR"]) == "pt_BR"
    assert fmt.extract_supported_locale_suffix("messages.pt-BR.json", ["pt-BR"]) == "pt-BR"
    assert fmt.extract_locale_suffix("messages.json") is None
    assert fmt.source_filename("wallet_pt_BR.json", ["pt_BR", "pt"]) == "wallet.json"
    assert fmt.source_filename("wallet.pt-BR.json", ["pt-BR", "pt"]) == "wallet.json"
