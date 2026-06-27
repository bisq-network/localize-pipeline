"""Tests for localization file layout metadata."""

from src.localization_formats import JSON_FORMAT, JAVA_PROPERTIES_FORMAT
from src.localization_layouts import (
    LOCALE_DIRECTORY_LAYOUT,
    LOCALE_FILENAME_LAYOUT,
    SUFFIX_LAYOUT,
    LocalizationLayout,
    load_localization_layout,
)


def test_suffix_layout_keeps_existing_properties_conventions():
    layout = SUFFIX_LAYOUT

    assert layout.extract_locale("messages_de.properties", ["de"], JAVA_PROPERTIES_FORMAT) == "de"
    assert layout.is_target_file("nested/messages_pt_BR.properties", ["pt_BR"], JAVA_PROPERTIES_FORMAT)
    assert not layout.is_target_file("nested/messages.properties", ["pt_BR"], JAVA_PROPERTIES_FORMAT)
    assert layout.is_source_file("nested/messages.properties", ["pt_BR"], JAVA_PROPERTIES_FORMAT)
    assert layout.source_path_for_target(
        "nested/messages_pt_BR.properties",
        ["pt_BR"],
        JAVA_PROPERTIES_FORMAT,
    ) == "nested/messages.properties"


def test_locale_directory_layout_maps_locale_segment_to_source_locale():
    layout = LocalizationLayout(id="locale_directory", source_locale="en")

    assert layout.extract_locale("locales/de/common.json", ["de", "fr"], JSON_FORMAT) == "de"
    assert layout.is_target_file("locales/de/common.json", ["de", "fr"], JSON_FORMAT)
    assert not layout.is_target_file("locales/en/common.json", ["de", "fr"], JSON_FORMAT)
    assert layout.is_source_file("locales/en/common.json", ["de", "fr"], JSON_FORMAT)
    assert layout.source_path_for_target(
        "locales/de/common.json",
        ["de", "fr"],
        JSON_FORMAT,
    ) == "locales/en/common.json"


def test_locale_filename_layout_maps_locale_filename_to_source_filename():
    layout = LocalizationLayout(id="locale_filename", source_locale="en")

    assert layout.extract_locale("src/i18n/de.json", ["de", "fr"], JSON_FORMAT) == "de"
    assert layout.is_target_file("src/i18n/de.json", ["de", "fr"], JSON_FORMAT)
    assert layout.is_source_file("src/i18n/en.json", ["de", "fr"], JSON_FORMAT)
    assert layout.source_path_for_target("src/i18n/fr.json", ["de", "fr"], JSON_FORMAT) == "src/i18n/en.json"


def test_load_localization_layout_accepts_registry_ids_and_overrides():
    assert load_localization_layout(None) == SUFFIX_LAYOUT
    assert load_localization_layout("suffix") == SUFFIX_LAYOUT
    assert load_localization_layout("locale_directory") == LOCALE_DIRECTORY_LAYOUT
    assert load_localization_layout("locale_filename") == LOCALE_FILENAME_LAYOUT

    layout = load_localization_layout({"id": "locale_directory", "source_locale": "en-US"})

    assert layout.id == "locale_directory"
    assert layout.source_locale == "en-US"
