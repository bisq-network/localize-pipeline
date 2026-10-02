"""Replay reviewed Italian trade terminology without requiring every inflection."""

import json
import subprocess
from pathlib import Path

import pytest

from localize.semantic_quality import TranslationChange, evaluate_semantic_rules
from localize.translation_quality_gate import load_quality_gate_config, main

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE = json.loads(
    (PROJECT_ROOT / "tests/fixtures/semantic_rules/italian_trade_failures.json").read_text(encoding="utf-8")
)
RULE_ID = "it-trade-failure-terminology"


def _write_properties(path, values):
    """Write the exact reviewed property values, including their placeholders."""
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


@pytest.mark.parametrize("profile", ["bisq", "bisq-mobile"])
@pytest.mark.parametrize("scope", ["changed", "all"])
@pytest.mark.parametrize("mixed_term", [None, "transazione", "commercio"])
def test_reviewed_italian_failures_block_and_corrections_pass(tmp_path, profile, scope, mixed_term):
    """Block reviewed defects and mixed terminology, then accept clean corrections."""
    resources = tmp_path / "resources"
    resources.mkdir()
    source = {item["key"]: item["source"] for item in FIXTURE["failures"] + FIXTURE["valid_verbs"]}
    _write_properties(resources / "bisq_easy.properties", source)
    target = resources / "bisq_easy_it.properties"
    target.touch()

    def git(*args):
        """Exercise the real staged-diff reader without modifying a live repository."""
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("add", "resources")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Seed locale fixture")
    verbs = {item["key"]: item["target"] for item in FIXTURE["valid_verbs"]}
    original = {
        item["key"]: (
            item["corrected"].replace("scambio", f"scambio ({mixed_term})")
            if mixed_term else item["original"]
        )
        for item in FIXTURE["failures"]
    }
    _write_properties(target, {**original, **verbs})
    git("add", "resources")
    report_path = tmp_path / "report.json"
    args = [
        "--repo-root", str(tmp_path), "--input-folder", str(resources),
        "--config", str(PROJECT_ROOT / "profiles" / profile / "config.yaml"),
        "--validation-summary", str(tmp_path / "missing-summary.json"),
        "--output-json", str(report_path),
        "--output-markdown", str(tmp_path / "report.md"),
        "--audit-scope", scope,
        "--changed-files", "resources/bisq_easy_it.properties",
    ]
    assert main(args) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["semantic_qa"]["errors_count"] == 4
    assert {item["key"] for item in report["semantic_qa"]["examples"]} == set(original)
    assert {item["rule_id"] for item in report["semantic_qa"]["examples"]} == {RULE_ID}
    corrected = {item["key"]: item["corrected"] for item in FIXTURE["failures"]}
    _write_properties(target, {**corrected, **verbs})
    git("add", "resources")
    assert main(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["semantic_qa"]["errors_count"] == 0


@pytest.mark.parametrize("profile", ["bisq", "bisq-mobile"])
def test_rule_keeps_source_key_and_locale_boundaries(profile):
    """Transactions, other locales and unreviewed keys remain outside this policy."""
    _, _, _, rules = load_quality_gate_config(str(PROJECT_ROOT / "profiles" / profile / "config.yaml"))
    scoped_rules = [rule for rule in rules if rule.id == RULE_ID]
    assert len(scoped_rules) == 1
    changes = []
    for item in FIXTURE["failures"]:
        for key, locale, source in [
            (item["key"], "it", item["source"].replace("trade", "transaction")),
            (item["key"], "fr", item["source"]),
            (item["key"] + ".unreviewed", "it", item["source"]),
        ]:
            changes.append(TranslationChange(
                file=f"bisq_easy_{locale}.properties", locale_code=locale, key=key,
                source_value=source, old_value=None, new_value=item["original"],
            ))
    assert evaluate_semantic_rules(changes, scoped_rules) == []
