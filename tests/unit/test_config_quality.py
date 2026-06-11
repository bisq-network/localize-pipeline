"""Regression tests for production translation prompt guidance."""
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "config_path",
    [
        "config.example.yaml",
        "docker/config.docker.yaml",
    ],
)
def test_recent_coderabbit_translation_nits_are_encoded_as_style_rules(config_path):
    """Keep prompt rules aligned with translation issues found in reviewed PRs."""
    config = yaml.safe_load((PROJECT_ROOT / config_path).read_text(encoding="utf-8"))
    style_rules = config["style_rules"]
    semantic_review = config.get("semantic_review", {})
    semantic_rules = config.get("semantic_quality_rules", [])
    assert all("id" in rule for rule in semantic_rules)
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
    assert {
        "trade-history-traders-label",
        "af-trade-history-counts-trades",
        "vi-clearnet-clear-network-address",
    }.issubset(semantic_rules_by_id)
    assert all(
        semantic_rules_by_id[rule_id]["source"] == "bisq-mobile#1478 CodeRabbit"
        for rule_id in {
            "trade-history-traders-label",
            "af-trade-history-counts-trades",
            "vi-clearnet-clear-network-address",
        }
    )
    assert any(
        "paymentAccounts.details" in rule
        and "paymentAccounts.accountCreationDate" in rule
        for rule in style_rules["pcm"]
    )
