from localize.translation_validator import find_glossary_mismatches
from localize.translate_localization_files import (
    _build_holistic_review_system_prompt,
    _glossary_for_deterministic_enforcement,
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


def test_holistic_review_prompt_allows_grammatical_inflection():
    prompt = _build_holistic_review_system_prompt(
        target_language="Russian",
        keys_to_review=["Entry"],
        source_content="Entry=Entry",
        translated_content="Entry=Запись",
        style_rules_text="",
        translation_glossary={"entry": "запись"},
        translation_glossary_enforcement="prompt-only",
    )

    assert "Preferred Translation Glossary" in prompt
    assert "preferred base term" in prompt
    assert "inflect or adapt" in prompt
    assert "must contain" not in prompt


def test_holistic_review_prompt_requests_sibling_terminology_consistency():
    prompt = _build_holistic_review_system_prompt(
        target_language="Estonian",
        keys_to_review=["user.bondedRoles.registration.proposalTxId"],
        source_content="user.bondedRoles.registration.proposalTxId=Proposal transaction ID",
        translated_content="user.bondedRoles.registration.proposalTxId=Ettepaneku tehingu ID",
        style_rules_text="",
    )

    # The reviewer sees the whole translated file, so it is the stage that can
    # align a concept named inline in a step-by-step instruction with its
    # dedicated field/button label sharing the same dotted key prefix. Drafting
    # cannot: co-changed siblings are translated concurrently and are absent
    # from each other's context (bisq2#4962 reputation registration drift).
    assert "Terminology Consistency Across Related Keys" in prompt
    assert "same dotted prefix" in prompt
    assert "only some of the related keys are in your review scope" in prompt
    # Guidance must stay product-agnostic; no concrete product name leaks in.
    assert "Bisq" not in prompt


def test_prompt_only_glossary_is_not_exactly_enforced():
    glossary = {"entry": "запись"}

    assert _glossary_for_deterministic_enforcement(glossary, "exact") == glossary
    assert _glossary_for_deterministic_enforcement(glossary, "prompt-only") == {}


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
