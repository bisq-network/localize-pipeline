"""Tests for reusable placeholder token rules."""

import asyncio
from collections import Counter

import pytest

from localize.placeholder_rules import (
    extract_placeholder_tokens,
    placeholder_profile,
    protect_placeholders,
    restore_placeholders,
    set_placeholder_profile,
)
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


def test_literal_percent_prose_is_not_a_placeholder():
    source = "Trade price must be greater than -10% of market price"
    target = "Handelspreis muss größer als -10% des Marktpreises sein"

    assert extract_placeholder_tokens(source) == Counter()
    assert extract_placeholder_tokens(target) == Counter()
    assert check_placeholder_parity(source, target)


def test_percent_after_brace_placeholder_is_literal_in_prose():
    source = "Starting Tor {0}%"
    target = "Tor {0}% başlatılıyor"

    assert extract_placeholder_tokens(source) == Counter({"{0}": 1})
    assert extract_placeholder_tokens(target) == Counter({"{0}": 1})
    assert check_placeholder_parity(source, target)


def test_json_structural_braces_are_not_placeholders():
    source = '{\n  "/title": "Title"\n}'
    target = '{\n  "/title": "Titel"\n}'

    assert extract_placeholder_tokens(source) == Counter()
    assert check_placeholder_parity(source, target)


def test_default_profile_ignores_java_indexed_tokens():
    # Bisq behavior must be unchanged: %0-style tokens are not placeholders
    # unless the java-indexed profile is opted into.
    assert extract_placeholder_tokens("%0 of %1 entries cleaned up.") == Counter()


def test_java_indexed_profile_detects_percent_number_tokens():
    with placeholder_profile("java-indexed"):
        assert extract_placeholder_tokens("%0 of %1 entries cleaned up.") == Counter(
            {"%0": 1, "%1": 1}
        )
        assert extract_placeholder_tokens("Push to %0 was rejected (%1). %2 %3") == Counter(
            {"%0": 1, "%1": 1, "%2": 1, "%3": 1}
        )
        assert extract_placeholder_tokens("[Search documentation](%0)") == Counter({"%0": 1})


def test_java_indexed_profile_keeps_existing_syntaxes_winning():
    with placeholder_profile("java-indexed"):
        text = "Hello {0}, {{name}}, %1$d, %(amount).2f, %s, <b>bold</b>"
        assert extract_placeholder_tokens(text) == Counter(
            {
                "{0}": 1,
                "{{name}}": 1,
                "%1$d": 1,
                "%(amount).2f": 1,
                "%s": 1,
                "<b>": 1,
                "</b>": 1,
            }
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("%0e", Counter({"%0e": 1})),
        ("%0d", Counter({"%0d": 1})),
        ("%1x", Counter({"%1x": 1})),
        ("%10", Counter({"%10": 1})),
        ("%0%1", Counter({"%0": 1, "%1": 1})),
        ("%%0", Counter({"%0": 1})),
        ("50%0", Counter({"%0": 1})),
        ("%0.5", Counter({"%0": 1})),
        ("final %", Counter()),
        ('<b data-value="%0">text</b>', Counter({'<b data-value="%0">': 1, "</b>": 1})),
        ("{{value %0}}", Counter({"{{value %0}}": 1})),
    ],
)
def test_java_indexed_profile_adjacency_and_outer_token_precedence(text, expected):
    with placeholder_profile("java-indexed"):
        assert extract_placeholder_tokens(text) == expected


def test_standard_profile_keeps_legacy_percent_edge_behavior():
    assert extract_placeholder_tokens("%0e %0d %1x %10 %0%1 %%0 50%0 %0.5 final %") == Counter(
        {"%0e": 1, "%0d": 1, "%1x": 1, "%0%": 1}
    )


def test_java_indexed_profile_handles_adjacent_indexed_tokens():
    with placeholder_profile("java-indexed"):
        assert extract_placeholder_tokens("%0%1") == Counter({"%0": 1, "%1": 1})
        protected, mapping = protect_placeholders("%0%1")

    assert set(mapping.values()) == {"%0", "%1"}
    assert restore_placeholders(protected, mapping) == "%0%1"


def test_java_indexed_profile_leaves_literal_percent_prose_alone():
    with placeholder_profile("java-indexed"):
        assert extract_placeholder_tokens("Discount 50% off") == Counter()
        assert extract_placeholder_tokens("100%") == Counter()
        assert extract_placeholder_tokens("greater than -10% of market price") == Counter()


def test_java_indexed_profile_parity_catches_dropped_token():
    with placeholder_profile("java-indexed"):
        assert check_placeholder_parity("%0 of %1 entries", "%0 von %1 Einträgen")
        assert not check_placeholder_parity("%0 of %1 entries", "von %1 Einträgen")


def test_java_indexed_profile_protects_and_restores_tokens():
    with placeholder_profile("java-indexed"):
        original = "Push to %0 was rejected (%1)."
        protected, mapping = protect_placeholders(original)
        assert "%0" not in protected
        assert "%1" not in protected
        assert set(mapping.values()) == {"%0", "%1"}
        assert restore_placeholders(protected, mapping) == original


def test_java_indexed_profile_preserves_placeholder_multiplicity_round_trip():
    original = "%0 then %0 and finally %1"

    with placeholder_profile("java-indexed"):
        assert extract_placeholder_tokens(original) == Counter({"%0": 2, "%1": 1})
        protected, mapping = protect_placeholders(original)

    assert list(mapping.values()).count("%0") == 2
    assert restore_placeholders(protected, mapping) == original


def test_placeholder_profile_resets_after_context_exit():
    with placeholder_profile("java-indexed"):
        assert extract_placeholder_tokens("%0") == Counter({"%0": 1})
    assert extract_placeholder_tokens("%0") == Counter()


def test_placeholder_profile_resets_after_exception():
    with pytest.raises(RuntimeError):
        with placeholder_profile("java-indexed"):
            raise RuntimeError("boom")

    assert extract_placeholder_tokens("%0") == Counter()


@pytest.mark.asyncio
async def test_placeholder_profiles_are_isolated_between_async_tasks():
    ready = asyncio.Event()

    async def java_task():
        with placeholder_profile("java-indexed"):
            ready.set()
            await asyncio.sleep(0)
            return extract_placeholder_tokens("%0")

    async def standard_task():
        await ready.wait()
        return extract_placeholder_tokens("%0")

    java_tokens, standard_tokens = await asyncio.gather(java_task(), standard_task())

    assert java_tokens == Counter({"%0": 1})
    assert standard_tokens == Counter()


def test_set_placeholder_profile_rejects_unknown_profile():
    with pytest.raises(ValueError):
        set_placeholder_profile("no-such-profile")


def test_protect_and_restore_common_placeholder_tokens():
    original = "Hello {{name}}, see <a href=\"{0}\">%s</a>."

    protected, mapping = protect_placeholders(original)
    restored = restore_placeholders(protected, mapping)

    assert restored == original
    assert original != protected
    assert set(mapping.values()) == {"{{name}}", "<a href=\"{0}\">", "%s", "</a>"}
