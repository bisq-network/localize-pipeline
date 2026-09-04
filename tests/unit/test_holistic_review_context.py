from localize.localization_formats import JAVA_PROPERTIES_FORMAT
from localize.translate_localization_files import (
    _select_holistic_review_context_keys,
)


def test_context_selects_only_localized_dotted_siblings_in_strength_order():
    source = {
        "account.title": "Account",
        "account.security.password": "Password",
        "account.security.confirm": "Confirm password",
        "account.security.empty": "Empty value",
        "accounting.security.title": "Accounting security",
        "network.security.title": "Network security",
    }
    translated = {
        "account.title": "Konto",
        "account.security.password": "Passwort",
        "account.security.confirm": "Confirm password",
        "account.security.empty": "",
        "accounting.security.title": "Buchhaltungssicherheit",
        "network.security.title": "Netzwerksicherheit",
    }

    selected = _select_holistic_review_context_keys(
        source,
        translated,
        ["account.security.reset"],
        JAVA_PROPERTIES_FORMAT,
    )

    assert selected == ["account.security.password", "account.title"]


def test_context_preserves_file_order_for_equally_related_siblings():
    source = {
        "settings.first": "First",
        "settings.second": "Second",
        "settings.third": "Third",
    }
    translated = {
        "settings.second": "Zweite",
        "settings.first": "Erste",
        "settings.third": "Dritte",
    }

    selected = _select_holistic_review_context_keys(
        source,
        translated,
        ["settings.current"],
        JAVA_PROPERTIES_FORMAT,
    )

    assert selected == ["settings.second", "settings.first", "settings.third"]


def test_context_enforces_key_and_utf8_byte_bounds_but_keeps_looking():
    source = {
        "dialog.large": "L" * 200,
        "dialog.short": "Short",
        "dialog.extra": "Extra",
    }
    translated = {
        "dialog.large": "Г" * 200,
        "dialog.short": "Kurz",
        "dialog.extra": "Zusatz",
    }

    selected = _select_holistic_review_context_keys(
        source,
        translated,
        ["dialog.current"],
        JAVA_PROPERTIES_FORMAT,
        max_context_keys=1,
        max_context_utf8_bytes=64,
    )

    assert selected == ["dialog.short"]


def test_context_never_repeats_a_reviewed_key():
    source = {
        "menu.open": "Open",
        "menu.close": "Close",
    }
    translated = {
        "menu.open": "Oeffnen",
        "menu.close": "Schliessen",
    }

    selected = _select_holistic_review_context_keys(
        source,
        translated,
        ["menu.open"],
        JAVA_PROPERTIES_FORMAT,
    )

    assert selected == ["menu.close"]


def test_context_excludes_fresh_keys_from_other_review_chunks():
    source = {
        "account.security.title": "Security",
        "account.security.reset": "Reset password",
        "account.security.rotate": "Rotate password",
    }
    translated = {
        "account.security.title": "Sicherheit",
        "account.security.reset": "Passwort zuruecksetzen",
        "account.security.rotate": "Passwort rotieren",
    }

    selected = _select_holistic_review_context_keys(
        source,
        translated,
        ["account.security.reset"],
        JAVA_PROPERTIES_FORMAT,
        excluded_context_keys={
            "account.security.reset",
            "account.security.rotate",
        },
    )

    assert selected == ["account.security.title"]
