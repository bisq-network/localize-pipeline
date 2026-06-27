"""Tests for configured localization format/layout profiles."""

import pytest

from src.localization_formats import JSON_FORMAT, JAVA_PROPERTIES_FORMAT
from src.localization_layouts import LocalizationLayout, SUFFIX_LAYOUT
from src.localization_profiles import LocalizationProfile, load_localization_profiles


def test_load_localization_profiles_defaults_to_legacy_single_format():
    profiles = load_localization_profiles({})

    assert profiles == (
        LocalizationProfile(
            localization_format=JAVA_PROPERTIES_FORMAT,
            localization_layout=SUFFIX_LAYOUT,
        ),
    )


def test_load_localization_profiles_preserves_singular_config_shape():
    profiles = load_localization_profiles({
        "localization_format": "json",
        "localization_layout": {"id": "locale_directory", "source_locale": "en"},
    })

    assert profiles == (
        LocalizationProfile(
            localization_format=JSON_FORMAT,
            localization_layout=LocalizationLayout(id="locale_directory", source_locale="en"),
        ),
    )


def test_load_localization_profiles_preserves_custom_format_mapping():
    profiles = load_localization_profiles({
        "localization_format": {
            "id": "custom_json",
            "display_name": "Custom JSON",
            "file_extension": ".json",
            "code_fence": "json",
            "locale_suffix_regex": r"_([a-z]{2})\.json$",
        },
        "localization_layout": {"id": "suffix", "source_locale": "en"},
    })

    assert profiles[0].localization_format.id == "custom_json"
    assert profiles[0].localization_format.file_extension == ".json"
    assert profiles[0].localization_layout.id == "suffix"


def test_load_localization_profiles_accepts_multiple_format_profiles():
    profiles = load_localization_profiles({
        "localization_formats": [
            {"id": "java_properties", "layout": "suffix"},
            {
                "id": "json",
                "layout": {"id": "locale_directory", "source_locale": "en"},
            },
        ],
    })

    assert profiles == (
        LocalizationProfile(JAVA_PROPERTIES_FORMAT, LocalizationLayout(id="suffix", source_locale="en")),
        LocalizationProfile(JSON_FORMAT, LocalizationLayout(id="locale_directory", source_locale="en")),
    )


def test_load_localization_profiles_rejects_empty_multi_format_config():
    with pytest.raises(ValueError, match="localization_formats must contain at least one profile"):
        load_localization_profiles({"localization_formats": []})
