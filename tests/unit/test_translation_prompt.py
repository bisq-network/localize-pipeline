"""Unit tests for reusable translation prompt construction."""

from localize.localization_formats import JAVA_PROPERTIES_FORMAT
from localize.translation_prompts import build_translation_system_prompt


def test_translation_system_prompt_is_generic_without_project_context():
    prompt = build_translation_system_prompt(
        target_language="German",
        style_rules_text="",
        project_context="",
        localization_format=JAVA_PROPERTIES_FORMAT,
    )

    assert "software localization" in prompt
    assert "Bisq" not in prompt
    assert "desktop trading app" not in prompt


def test_translation_system_prompt_includes_configured_project_context():
    prompt = build_translation_system_prompt(
        target_language="German",
        style_rules_text="",
        project_context="Translate for Acme Cloud's admin console.",
        localization_format=JAVA_PROPERTIES_FORMAT,
    )

    assert "Project Context" in prompt
    assert "Acme Cloud" in prompt


def test_translation_system_prompt_mentions_format_metadata():
    prompt = build_translation_system_prompt(
        target_language="German",
        style_rules_text="",
        project_context="",
        localization_format=JAVA_PROPERTIES_FORMAT,
    )

    assert JAVA_PROPERTIES_FORMAT.display_name in prompt


def test_translation_system_prompt_requests_grammatical_number_agreement():
    prompt = build_translation_system_prompt(
        target_language="German",
        style_rules_text="",
        project_context="",
        localization_format=JAVA_PROPERTIES_FORMAT,
    )

    assert "grammatical number" in prompt
    # Singular/plural count-template families must be called out explicitly so
    # the model does not reuse the plural wording for the singular case.
    assert ".single" in prompt
    assert ".plural" in prompt
    assert "identical singular and plural target forms are acceptable" in prompt
    # The example must render a real placeholder, not a doubled f-string brace.
    assert "Used {0} time" in prompt
    assert "{{0}}" not in prompt


def test_translation_system_prompt_warns_against_compound_splitting():
    prompt = build_translation_system_prompt(
        target_language="Norwegian",
        style_rules_text="",
        project_context="",
        localization_format=JAVA_PROPERTIES_FORMAT,
    )

    # Compound-building languages must keep closed compounds joined rather than
    # splitting them (Norwegian saerskriving), which recurred in wallet keys.
    assert "compound noun" in prompt
    assert "Adressenotat" in prompt
    assert "Adresse notat" in prompt
