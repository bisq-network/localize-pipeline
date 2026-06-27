import pytest

from localize.localization_adapters import (
    JSON_ADAPTER,
    JAVA_PROPERTIES_ADAPTER,
    LocalizationFileAdapter,
    get_localization_adapter,
    list_localization_adapters,
    register_localization_adapter,
    unregister_localization_adapter,
)
from localize.localization_formats import LocalizationFormat, load_localization_format


def _custom_adapter(format_id: str = "yaml_test") -> LocalizationFileAdapter:
    localization_format = LocalizationFormat(
        id=format_id,
        display_name="YAML test",
        file_extension=".yaml",
        code_fence="yaml",
        locale_suffix_regex=r".*_([a-z]{2})\.yaml$",
    )
    return LocalizationFileAdapter(
        localization_format=localization_format,
        parse_file=lambda _path: ([], {}),
        reassemble_file=lambda _lines: "",
        synchronize_keys=lambda _source, _target: (set(), set()),
        lint_file=lambda _path: [],
        extract_changed_key_from_diff_line=lambda _line: None,
        build_review_content=lambda _translations, _keys: "",
        escape_translation=lambda _source, value: value,
    )


def test_builtin_adapters_are_registered_by_default():
    adapters = list_localization_adapters()

    assert adapters[JAVA_PROPERTIES_ADAPTER.localization_format.id] is JAVA_PROPERTIES_ADAPTER
    assert adapters[JSON_ADAPTER.localization_format.id] is JSON_ADAPTER
    assert get_localization_adapter(JSON_ADAPTER.localization_format) is JSON_ADAPTER


def test_external_adapter_registration_exposes_format_lookup():
    adapter = _custom_adapter()

    try:
        register_localization_adapter(adapter)

        assert get_localization_adapter(adapter.localization_format) is adapter
        assert load_localization_format("yaml_test") == adapter.localization_format
        assert list_localization_adapters()["yaml_test"] is adapter
    finally:
        unregister_localization_adapter("yaml_test")


def test_adapter_registration_requires_explicit_replace():
    adapter = _custom_adapter("dupe_test")
    replacement = _custom_adapter("dupe_test")

    try:
        register_localization_adapter(adapter)

        with pytest.raises(ValueError, match="already registered"):
            register_localization_adapter(replacement)

        register_localization_adapter(replacement, replace=True)
        assert get_localization_adapter(replacement.localization_format) is replacement
    finally:
        unregister_localization_adapter("dupe_test")


def test_builtin_adapters_cannot_be_unregistered():
    with pytest.raises(ValueError, match="built-in"):
        unregister_localization_adapter(JAVA_PROPERTIES_ADAPTER.localization_format.id)
