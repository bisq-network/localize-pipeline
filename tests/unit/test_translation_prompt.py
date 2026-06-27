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
