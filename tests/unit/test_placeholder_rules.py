"""Tests for reusable placeholder token rules."""

from collections import Counter

from localize.placeholder_rules import extract_placeholder_tokens, protect_placeholders, restore_placeholders
from localize.translation_validator import check_placeholder_parity


def test_extract_placeholder_tokens_supports_common_localization_syntaxes():
    text = "Hello {0}, {0,choice,0#none|1#one}, {{name}}, %1$d, %(amount).2f, %s, <b>bold</b>"

    assert extract_placeholder_tokens(text) == Counter(
        {
            "{0}": 1,
            "{0,choice,0#none|1#one}": 1,
            "{{name}}": 1,
            "%1$d": 1,
            "%(amount).2f": 1,
            "%s": 1,
            "<b>": 1,
            "</b>": 1,
        }
    )


def test_placeholder_parity_allows_reordered_common_tokens():
    source = "Pay {{amount}} to %1$s before {0}."
    target = "Vor {0} {{amount}} an %1$s zahlen."

    assert check_placeholder_parity(source, target)


def test_placeholder_parity_rejects_missing_i18next_token():
    assert not check_placeholder_parity("Hello {{name}}", "Hallo")


def test_json_structural_braces_are_not_placeholders():
    source = '{\n  "/title": "Title"\n}'
    target = '{\n  "/title": "Titel"\n}'

    assert extract_placeholder_tokens(source) == Counter()
    assert check_placeholder_parity(source, target)


def test_protect_and_restore_common_placeholder_tokens():
    original = "Hello {{name}}, see <a href=\"{0}\">%s</a>."

    protected, mapping = protect_placeholders(original)
    restored = restore_placeholders(protected, mapping)

    assert restored == original
    assert original != protected
    assert set(mapping.values()) == {"{{name}}", "<a href=\"{0}\">", "%s", "</a>"}
