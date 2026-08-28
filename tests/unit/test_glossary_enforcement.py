from localize.translation_validator import find_glossary_mismatches
from localize.translate_localization_files import (
    _build_holistic_review_system_prompt,
    _prefer_glossary_compliant_draft,
    load_glossary,
    run_post_translation_validation,
    run_per_key_validation_with_summary,
)


def test_glossary_compliance_requires_verbatim_target_mapping():
    glossary = {"entry": "Eintrag"}

    assert find_glossary_mismatches("Delete entry", "Eintrag löschen", glossary) == []
    assert find_glossary_mismatches("Delete entry", "Element löschen", glossary) == [
        ("entry", "Eintrag")
    ]


def test_glossary_compliance_requires_target_mapping_at_word_boundaries():
    glossary = {"entry": "Eintrag"}

    assert find_glossary_mismatches("Delete entry", "Eintragsdetails löschen", glossary) == [
        ("entry", "Eintrag")
    ]
    assert find_glossary_mismatches("Delete entry", "(Eintrag) löschen", glossary) == []


def test_glossary_compliance_counts_repeated_target_mappings_at_boundaries():
    glossary = {"entry": "Eintrag"}

    assert find_glossary_mismatches(
        "Merge entry into entry",
        "Eintrag mit Eintragsdetails zusammenführen",
        glossary,
    ) == [("entry", "Eintrag")]
    assert find_glossary_mismatches(
        "Merge entry into entry",
        "Eintrag mit Eintrag zusammenführen",
        glossary,
    ) == []


def test_glossary_compliance_is_case_insensitive_at_word_boundaries():
    glossary = {"merge": "zusammenführen"}

    assert find_glossary_mismatches("Merge entries", "Einträge zusammenführen", glossary) == []
    assert find_glossary_mismatches("Merged entries", "Einträge wurden zusammengeführt", glossary) == []


def test_glossary_compliance_prefers_longest_overlapping_source_term():
    glossary = {
        "AI functionality": "KI-Funktionen",
        "functionality": "Funktionen",
    }

    assert find_glossary_mismatches(
        "Enable AI functionality",
        "KI-Funktionen aktivieren",
        glossary,
    ) == []


def test_per_key_validation_blocks_review_output_that_breaks_glossary():
    source = {"Delete entry": "Delete entry"}

    validated, summary = run_per_key_validation_with_summary(
        {"Delete entry": "Element löschen"},
        source,
        "Messages_de.properties",
        translation_glossary={"entry": "Eintrag"},
    )

    assert validated == source
    assert summary["glossary_mismatch_keys"] == ["Delete entry"]
    assert summary["glossary_failures_count"] == 1


def test_holistic_review_prompt_receives_mandatory_glossary():
    prompt = _build_holistic_review_system_prompt(
        target_language="Russian",
        keys_to_review=["Delete entry"],
        source_content="Delete entry=Delete entry",
        translated_content="Delete entry=Eintrag löschen",
        style_rules_text="",
        translation_glossary={"entry": "Eintrag"},
    )

    assert "Mandatory Translation Glossary" in prompt
    assert '"entry"' in prompt
    assert '"Eintrag"' in prompt


def test_compliant_draft_wins_when_review_breaks_glossary():
    assert _prefer_glossary_compliant_draft(
        "Delete entry",
        "Eintrag löschen",
        "Element löschen",
        {"entry": "Eintrag"},
    ) == "Eintrag löschen"


def test_post_validation_rejects_noncompliant_generated_value_but_allows_source_fallback():
    source = {"Delete entry": "Delete entry"}
    glossary = {"entry": "Eintrag"}

    assert not run_post_translation_validation(
        "Delete\\ entry=Element löschen\n",
        source,
        "Messages_de.properties",
        changed_keys_for_run={"Delete entry"},
        translation_glossary=glossary,
    )
    assert run_post_translation_validation(
        "Delete\\ entry=Delete entry\n",
        source,
        "Messages_de.properties",
        changed_keys_for_run={"Delete entry"},
        translation_glossary=glossary,
    )


def test_load_glossary_excludes_metadata_and_quarantine_namespaces(tmp_path):
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(
        '{"_comment":"metadata","de":{"entry":"Eintrag"},'
        '"_quarantine_de":{"citation":{"de":"Zitat"}}}',
        encoding="utf-8",
    )

    assert load_glossary(str(glossary_path)) == {"de": {"entry": "Eintrag"}}
