"""The Bisq 1 glossary reuses reviewed mappings without importing Bisq 2 roles."""

import json
from pathlib import Path

import pytest
import yaml

from localize.formats.validation import find_glossary_mismatches


ROOT = Path(__file__).resolve().parents[2]
ALIASES = {"pt-br": "pt_BR", "pt": "pt_PT", "zh-hans": "zh-Hans", "zh-hant": "zh-Hant"}
LEGACY_DE = {"balance": "Guthaben", "trade id": "Handels-ID"}


def load_glossary(profile):
    return json.loads((ROOT / "profiles" / profile / "glossary.json").read_text())


def test_glossary_covers_profile_locales_without_inventing_persian():
    config = yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())
    glossary = load_glossary("bisq1")
    assert set(glossary) == {locale["code"] for locale in config["supported_locales"]}
    assert glossary["fa"] == {}
    for locale, terms in glossary.items():
        assert isinstance(terms, dict)
        if locale != "fa":
            assert terms
        for source, target in terms.items():
            assert isinstance(source, str) and source.strip() == source and source
            assert isinstance(target, str) and target.strip() == target and target
            assert "_" not in source and "." not in source


def test_glossary_values_have_bisq2_or_documented_legacy_provenance():
    inherited = load_glossary("bisq")
    for locale, terms in load_glossary("bisq1").items():
        original = inherited.get(ALIASES.get(locale, locale), {})
        normalized = {term.replace("_", " "): target for term, target in original.items()}
        for term, target in terms.items():
            if locale == "de" and term in LEGACY_DE:
                assert target == LEGACY_DE[term]
            else:
                assert target == normalized[term]


@pytest.mark.parametrize("locale", ["de", "pt-br", "pt", "zh-hans", "zh-hant"])
def test_shared_phrase_mappings_match_real_english_values(locale):
    glossary = load_glossary("bisq1")[locale]
    # These are Bisq 1 English values, not its dotted/underscored property IDs.
    for phrase in ("Payment account", "Trade fee", "Transaction ID"):
        term = phrase.lower()
        target = glossary[term]
        assert not find_glossary_mismatches(phrase, target, {term: target})
        assert find_glossary_mismatches(phrase, "unrelated value", {term: target})


def test_bisq2_roles_and_profile_identity_are_not_global_rules():
    excluded = {"profile", "profile age", "reputation", "reputation score",
                "moderator", "release manager", "security manager", "signed witness",
                "market channel", "network.roles", "network.transport.headline.CLEAR"}
    for terms in load_glossary("bisq1").values():
        assert not excluded.intersection(terms)


def test_prompt_guidance_preserves_placeholder_controls_and_semantic_gates():
    from localize.translate_localization_files import (
        _glossary_for_deterministic_enforcement,
        run_per_key_validation_with_summary,
    )

    config = yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())
    assert config["translation_glossary_enforcement"] == "prompt-only"
    assert config["semantic_review"]["enabled"]
    assert config["quality_gate"]["block_on_semantic_qa_findings"]
    glossary = _glossary_for_deterministic_enforcement(
        load_glossary("bisq1")["de"], config["translation_glossary_enforcement"]
    )
    values = {"valid": "Handelskonto {0}", "placeholder": "Wallet", "control": "Wallet \x00 {0}"}
    _, summary = run_per_key_validation_with_summary(
        values, {"valid": "Account {0}", "placeholder": "Wallet {0}", "control": "Wallet {0}"},
        "displayStrings_de.properties", translation_glossary=glossary,
    )
    assert set(summary["failed_keys"]) == {"placeholder", "control"}
    assert summary["placeholder_mismatch_keys"] == ["placeholder"]
    assert summary["control_character_keys"] == ["control"]


def test_brands_preserve_attested_names_without_freezing_payment_descriptions():
    config = yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())
    brands = config["brand_technical_glossary"]
    assert len(brands) == len(set(brands))
    assert {"BSQ", "DAO", "Bisq Easy", "Liquid", "Amazon", "Revolut", "SWIFT"} <= set(brands)
    assert not {"MuSig", "Lightning", "I2P", "Wire Transfer", "US Money Order",
                "Amazon eGift Card", "Faster Payments",
                "SWIFT International Wire Transfer"}.intersection(brands)
