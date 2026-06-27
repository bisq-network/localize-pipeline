"""Unit tests for the shared locale-filename helpers."""
from localize.locale_utils import LOCALE_SUFFIX_RE, extract_locale_suffix, is_locale_file


class TestExtractLocaleSuffix:
    def test_simple_code(self):
        assert extract_locale_suffix("app_de.properties") == "de"

    def test_region_code_underscore(self):
        assert extract_locale_suffix("app_pt_BR.properties") == "pt_BR"

    def test_region_code_hyphen(self):
        assert extract_locale_suffix("messages_zh-Hant.properties") == "zh-Hant"

    def test_three_letter_code(self):
        assert extract_locale_suffix("ui_pcm.properties") == "pcm"

    def test_source_file_without_suffix_is_none(self):
        assert extract_locale_suffix("app.properties") is None

    def test_non_properties_is_none(self):
        assert extract_locale_suffix("app_de.txt") is None

    def test_non_locale_token_is_none(self):
        # 'test' is 4 lowercase letters -> not a valid 2-3 char language code
        assert extract_locale_suffix("app_test.properties") is None


class TestIsLocaleFile:
    def test_true_for_locale_file(self):
        assert is_locale_file("app_de.properties") is True

    def test_false_for_source_file(self):
        assert is_locale_file("app.properties") is False


def test_single_compiled_pattern_is_shared():
    # The module exposes one compiled regex used by both helpers.
    assert LOCALE_SUFFIX_RE.search("app_de.properties") is not None
    assert LOCALE_SUFFIX_RE.search("app.properties") is None
