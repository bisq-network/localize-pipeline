"""Regression tests for production translation prompt guidance."""
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BISQ_PROFILE_CONFIG = PROJECT_ROOT / "profiles" / "bisq" / "config.yaml"
BISQ_PROFILE_GLOSSARY = PROJECT_ROOT / "profiles" / "bisq" / "glossary.json"


def test_example_config_is_a_minimal_generic_starter():
    """config.example.yaml must stay small, generic, and git-source by default."""
    config = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert config["translation_source"] == "git"
    assert "target_project_root" in config and "input_folder" in config
    assert isinstance(config.get("supported_locales"), list) and config["supported_locales"]
    # It must NOT carry Bisq-specific project knowledge (that lives in the Bisq profile).
    assert "semantic_quality_rules" not in config
    assert {loc["code"] for loc in config["supported_locales"]} <= {"de", "es", "fr"}


def test_example_glossary_is_valid_and_small():
    glossary = yaml.safe_load((PROJECT_ROOT / "glossary.example.json").read_text(encoding="utf-8"))
    assert isinstance(glossary, dict) and glossary
    # keyed by language code -> {term: translation}
    for lang, terms in glossary.items():
        assert isinstance(terms, dict)


def test_bisq_profile_packages_config_and_glossary_assets():
    """Bisq-specific deployment knowledge lives in one profile directory."""
    assert BISQ_PROFILE_CONFIG.exists()
    assert BISQ_PROFILE_GLOSSARY.exists()

    config = yaml.safe_load(BISQ_PROFILE_CONFIG.read_text(encoding="utf-8"))
    glossary = yaml.safe_load(BISQ_PROFILE_GLOSSARY.read_text(encoding="utf-8"))

    assert config["localization_format"] == "java_properties"
    assert "Bisq" in config["project_context"]
    assert "semantic_quality_rules" in config
    assert config["glossary_file_path"] == "glossary.json"
    assert isinstance(glossary, dict) and glossary
    assert "de" in glossary


def test_docker_compose_mounts_the_selected_profile():
    compose = (PROJECT_ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "../profiles/${TRANSLATOR_PROFILE:-bisq}/config.yaml:/app/config.yaml:ro" in compose
    assert "../profiles/${TRANSLATOR_PROFILE:-bisq}/glossary.json:/app/glossary.json:ro" in compose


def test_update_service_watches_profile_assets_for_restart():
    script = (PROJECT_ROOT / "update-service.sh").read_text(encoding="utf-8")

    assert "profiles/" in script
    assert "profiles/bisq/config.yaml" in script
    assert "profiles/bisq/glossary.json" in script


def test_deployment_guide_documents_relative_input_folder_semantics():
    guide = (PROJECT_ROOT / "docs" / "new-project-deployment.md").read_text(encoding="utf-8")

    assert "# - input_folder: i18n/src/main/resources" in guide
    assert "input_folder is resolved relative to target_project_root" in guide
    assert "The paths must be absolute paths inside the container." not in guide


# config.example.yaml is intentionally a minimal generic starter; the Bisq
# project knowledge (style + semantic rules) lives in the Bisq profile config.
@pytest.mark.parametrize(
    "config_path",
    [
        "profiles/bisq/config.yaml",
    ],
)
def test_recent_coderabbit_translation_nits_are_encoded_as_style_rules(config_path):
    """Keep prompt rules aligned with translation issues found in reviewed PRs."""
    config = yaml.safe_load((PROJECT_ROOT / config_path).read_text(encoding="utf-8"))
    style_rules = config["style_rules"]
    semantic_review = config.get("semantic_review", {})
    semantic_rules = config.get("semantic_quality_rules", [])
    retained_allowlist = config.get("quality_gate", {}).get("retained_source_word_allowlist", {})
    supported_locale_codes = {
        locale["code"]
        for locale in config["supported_locales"]
    }
    semantic_rule_locale_codes = {
        locale
        for rule in semantic_rules
        for locale in rule.get("locales", [])
        if locale != "*"
    }
    assert all("id" in rule for rule in semantic_rules)
    assert semantic_rule_locale_codes <= supported_locale_codes
    semantic_rules_by_id = {
        rule["id"]: rule
        for rule in semantic_rules
    }

    assert any(
        "Cara kerjanya" in rule and "Cara kerja ini" in rule
        for rule in style_rules["id"]
    )
    assert any(
        "('n" in rule and "parenthesized" in rule
        for rule in style_rules["af_ZA"]
    )
    assert any(
        "count trades" in rule and "handelaars" in rule
        for rule in style_rules["af_ZA"]
    )
    assert any(
        "Comerciantes / Rol" in rule and "Traders / Rol" in rule
        for rule in style_rules["es"]
    )
    assert any(
        "Commerçants / Rôle" in rule and "Traders / Rôle" in rule
        for rule in style_rules["fr"]
    )
    assert any(
        "clearnet/open network" in rule and "Địa chỉ mạng công khai" in rule
        for rule in style_rules["vi"]
    )
    assert semantic_review.get("enabled") is True
    assert semantic_review.get("model") == "gpt-5.4-mini"
    assert "version" in retained_allowlist["da"]
    assert "version" in retained_allowlist["de"]
    assert {"information", "message", "messages", "version"}.issubset(retained_allowlist["fr"])
    assert "version" in retained_allowlist["sv"]
    assert {
        "trade-history-traders-label",
        "af-trade-history-counts-trades",
        "vi-clearnet-clear-network-address",
        "analytics-cta-truncated-reporting",
        "analytics-cta-reporting-loanword",
        "it-analytics-self-hosted",
        "fr-analytics-no-pii-negation",
        "el-tac-duplicate-compensation",
        "fi-tac-fused-all-information",
        "fr-tac-account-freeze-plural",
        "ga-tac-custody-customs",
        "ha-tac-compensation-computer",
        "mk-tac-central-entity-typo",
        "sl-tac-contributors-noun",
        "sr-tac-software-vulnerabilities",
        "th-tac-legal-representations",
        "yo-tac-risk-legal-headlines",
        "af-trade-guide-without-justification",
        "ca-trade-guide-exchange-account-details",
        "fi-trade-guide-mediator-term",
        "fr-trade-guide-trader-term",
        "ga-trade-guide-request-mediator",
        "hr-trade-guide-refund-term",
        "ta-trade-guide-counterparty-term",
    }.issubset(semantic_rules_by_id)
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1478 CodeRabbit"
        for rule_id in {
            "trade-history-traders-label",
            "af-trade-history-counts-trades",
            "vi-clearnet-clear-network-address",
        }
    )
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1484 CodeRabbit"
        for rule_id in {
            "it-analytics-self-hosted",
            "fr-analytics-no-pii-negation",
        }
    )
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1490 CodeRabbit"
        for rule_id in {
            "analytics-cta-truncated-reporting",
            "analytics-cta-reporting-loanword",
        }
    )
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq2#4835 CodeRabbit"
        for rule_id in {
            "el-tac-duplicate-compensation",
            "fi-tac-fused-all-information",
            "fr-tac-account-freeze-plural",
            "ga-tac-custody-customs",
            "ha-tac-compensation-computer",
            "mk-tac-central-entity-typo",
            "sl-tac-contributors-noun",
            "sr-tac-software-vulnerabilities",
            "th-tac-legal-representations",
            "yo-tac-risk-legal-headlines",
            "af-trade-guide-without-justification",
            "ca-trade-guide-exchange-account-details",
            "fi-trade-guide-mediator-term",
            "fr-trade-guide-trader-term",
            "ga-trade-guide-request-mediator",
            "hr-trade-guide-refund-term",
            "ta-trade-guide-counterparty-term",
        }
    )
    assert "personal" in retained_allowlist["es"]
    assert {"information", "message", "messages"}.issubset(retained_allowlist["fr"])
    assert "reporting" in retained_allowlist["it"]
    assert any(
        "paymentAccounts.details" in rule
        and "paymentAccounts.accountCreationDate" in rule
        for rule in style_rules["pcm"]
    )
