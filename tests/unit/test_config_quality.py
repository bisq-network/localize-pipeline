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

    assert any(
        "Cara kerjanya" in rule and "Cara kerja ini" in rule
        for rule in style_rules["id"]
    )
    assert any(
        "('n" in rule and "parenthesized" in rule
        for rule in style_rules["af_ZA"]
    )
    assert any(
        "paymentAccounts.details" in rule
        and "paymentAccounts.accountCreationDate" in rule
        for rule in style_rules["pcm"]
    )
