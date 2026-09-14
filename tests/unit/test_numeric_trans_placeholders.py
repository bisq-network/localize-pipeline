"""Numeric React-i18next component tags are structural placeholders."""

from collections import Counter

import pytest

from localize.placeholder_rules import (
    extract_placeholder_tokens,
    placeholder_profile,
    protect_placeholders,
    restore_placeholders,
    strip_placeholder_tokens,
)
from localize.translation_validator import check_placeholder_parity


@pytest.mark.parametrize("profile", ["standard", "java-indexed"])
def test_numeric_trans_tags_preserve_multiplicity_and_round_trip(profile):
    text = '<1>Read <3>{{name}}</3></1><1>again</1><5/><10 />'
    expected = Counter({'<1>': 2, '</1>': 2, '<3>': 1, '</3>': 1,
                        '{{name}}': 1, '<5/>': 1, '<10 />': 1})
    with placeholder_profile(profile):
        assert extract_placeholder_tokens(text) == expected
        protected, mapping = protect_placeholders(text)
        assert Counter(mapping.values()) == expected
        assert restore_placeholders(protected, mapping) == text
        assert strip_placeholder_tokens(text) == 'Read again'


@pytest.mark.parametrize('target', [
    '<1>Advertencia',                  # Missing closing tag.
    '<1>Advertencia<1/>',              # Closing replaced by self-closing.
    '<3>Advertencia</3>',              # Wrong component index.
    '<1>Advertencia</1></1>',          # Duplicated closing tag.
])
def test_numeric_trans_parity_rejects_structural_changes(target):
    assert not check_placeholder_parity('<1>Warning</1>', target)


def test_numeric_trans_parity_allows_translation_and_reordering():
    assert check_placeholder_parity('<1>Warning</1> <3>Help</3>', '<3>Ayuda</3> <1>Aviso</1>')


@pytest.mark.parametrize('text', ['1 < 3 > 2', '<3.14>', '<1word>', '<1\n>', '<1 attr="x">'])
def test_numeric_trans_detection_does_not_consume_prose_or_invalid_tags(text):
    assert extract_placeholder_tokens(text) == Counter()


def test_numeric_trans_tags_do_not_split_outer_placeholders_or_named_html():
    # Preserve the outer interpolation token rather than splitting its content.
    assert extract_placeholder_tokens('{{label <1>}}') == Counter({'{{label <1>}}': 1})
    assert extract_placeholder_tokens('<b>{{value}}</b>') == Counter({'<b>': 1, '{{value}}': 1, '</b>': 1})
