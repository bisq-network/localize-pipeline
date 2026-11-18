"""Existing values must be checked even when no translation is requested."""
import json
import subprocess

import pytest
import yaml

from localize.formats.metadata import JAVA_PROPERTIES_FORMAT, JSON_FORMAT
from localize.ignore_keys import compile_ignore_key_patterns
from localize.translate_localization_files import run_pre_translation_validation, run_post_translation_validation
from localize.translation_quality_gate import main


@pytest.mark.parametrize("value", ["L'adresse {0}", "L'''adresse {0}", r"L\u0027adresse {0}"])
def test_pre_validation_rejects_unchanged_bad_apostrophe_without_rewriting(tmp_path, value):
    source = tmp_path / "app.properties"
    target = tmp_path / "app_fr.properties"
    source.write_text("old=Address {0}\nnew=Updated\n")
    original = f"old={value}\nnew=Modifié\n"
    target.write_text(original)
    errors, _ = run_pre_translation_validation(str(target), str(source), JAVA_PROPERTIES_FORMAT,
                                               git_changed_keys={"new"})
    assert any("MessageFormat" in error and "old" in error for error in errors)
    assert target.read_text() == original


def test_post_validation_rejects_unchanged_bad_apostrophe():
    assert not run_post_translation_validation("old=L'adresse {0}\n", {"old": "Address {0}"},
                                              "app_fr.properties", JAVA_PROPERTIES_FORMAT,
                                              changed_keys_for_run=set())


def test_non_messageformat_and_ignored_keys_are_unaffected(tmp_path):
    assert run_post_translation_validation("plain=L'adresse\nignored=L'adresse {0}\n",
                                          {"plain": "Address", "ignored": "Address {0}"},
                                          "app_fr.properties", JAVA_PROPERTIES_FORMAT,
                                          ignore_key_patterns=compile_ignore_key_patterns(["ignored"]))
    assert run_post_translation_validation(json.dumps({"text": "L'adresse {0}"}),
                                          {"/text": "Address {0}"}, "fr.json", JSON_FORMAT)


def test_publication_gate_checks_unchanged_values_in_submitted_files_only(tmp_path):
    repo = tmp_path / "repo"
    resources = repo / "i18n"
    resources.mkdir(parents=True)
    (resources / "app.properties").write_text("old=Address {0}\nnew=Hello\n")
    (resources / "app_fr.properties").write_text("old=L'adresse {0}\nnew=Bonjour\n")
    (resources / "app_de.properties").write_text("old=Die 'Adresse {0}\nnew=Hallo\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"supported_locales": ["fr", "de"],
                                     "quality_gate": {"block_on_semantic_qa_findings": False}}))
    output = tmp_path / "quality.json"
    args = ["--repo-root", str(repo), "--input-folder", "i18n", "--config", str(config),
            "--validation-summary", str(tmp_path / "missing.json"), "--output-json", str(output),
            "--output-markdown", str(tmp_path / "quality.md"), "--changed-files", "i18n/app_fr.properties"]
    assert main(args) == 1
    report = json.loads(output.read_text())
    assert report["messageformat"]["failures"] == [{"file": "app_fr.properties", "key": "old", "side": "translation"}]
    (resources / "app_fr.properties").write_text("old=L''adresse {0}\nnew=Bonjour\n")
    assert main(args) == 0


def test_source_only_change_triggers_check_without_semantic_rules(tmp_path):
    repo = tmp_path / "repo"
    resources = repo / "i18n"
    resources.mkdir(parents=True)
    source = resources / "app.properties"
    source.write_text("old=Address\n")
    (resources / "app_fr.properties").write_text("old=L'adresse\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture"], check=True)
    source.write_text("old=Address {0}\n")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"supported_locales": ["fr"]}))
    output = tmp_path / "quality.json"
    args = ["--repo-root", str(repo), "--input-folder", "i18n", "--config", str(config),
            "--validation-summary", str(tmp_path / "missing.json"), "--output-json", str(output),
            "--output-markdown", str(tmp_path / "quality.md"), "--source-diff-base", "HEAD"]
    assert main(args) == 1
    report = json.loads(output.read_text())
    assert report["messageformat"]["failures"] == [{"file": "app_fr.properties", "key": "old", "side": "translation"}]


def test_per_key_summary_retains_apostrophe_failure_and_json_is_excluded():
    from localize.translate_localization_files import run_per_key_validation_with_summary
    from localize.translation_quality_gate import build_quality_gate_report, QualityGateConfig, SourceIdenticalStats

    source = {"old": "Address {0}"}
    target = {"old": "L'adresse {0}"}
    _, summary = run_per_key_validation_with_summary(target, source, "app_fr.properties",
                                                    localization_format=JAVA_PROPERTIES_FORMAT)
    assert summary["messageformat_keys"] == ["old"]
    report = build_quality_gate_report(SourceIdenticalStats(), None, {"files": {"app_fr.properties": summary}},
                                       ["i18n/app_fr.properties"], "i18n", QualityGateConfig())
    assert report["blocking"]
    assert report["validation"]["messageformat_failures_count"] == 1
    values, summary = run_per_key_validation_with_summary(target, source, "fr.json", localization_format=JSON_FORMAT)
    assert values == target
    assert summary["failed_keys"] == []


@pytest.mark.parametrize("staged_deletion", [False, True])
def test_publication_only_skips_missing_targets_when_deletion_is_staged(tmp_path, staged_deletion):
    from localize.formats.layouts import SUFFIX_LAYOUT
    from localize.formats.profiles import LocalizationProfile
    from localize.translation_quality_gate import audit_messageformat_apostrophes

    repo = tmp_path / "repo"
    resources = repo / "i18n"
    resources.mkdir(parents=True)
    (resources / "app.properties").write_text("text=Address {0}\n")
    target = resources / "app_fr.properties"
    target.write_text("text=L''adresse {0}\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture"], check=True)
    if staged_deletion:
        subprocess.run(["git", "-C", str(repo), "rm", "--", "i18n/app_fr.properties"], check=True)
    else:
        target.unlink()
    args = (str(repo), "i18n", ["fr"], [LocalizationProfile(JAVA_PROPERTIES_FORMAT, SUFFIX_LAYOUT)],
            ["i18n/app_fr.properties"])
    if staged_deletion:
        assert audit_messageformat_apostrophes(*args)["checked_entries_count"] == 0
    else:
        with pytest.raises(FileNotFoundError):
            audit_messageformat_apostrophes(*args)
