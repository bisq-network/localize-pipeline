"""Sidecars must hydrate source values when launched outside the target checkout."""
import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from localize.formats.metadata import JAVA_PROPERTIES_FORMAT
from localize.formats.layouts import SUFFIX_LAYOUT
from localize.formats.profiles import LocalizationProfile
from localize.semantic_quality import iter_all_translation_entries, iter_translation_changes_from_diff
from localize.translation_quality_gate import get_staged_diff, main, source_affected_translation_entries


@pytest.fixture
def external_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "target"
    resources = repo / "core/src/main/resources/i18n"
    resources.mkdir(parents=True)
    source = {f"message.{i}": f"English source sentence {i}." for i in range(159)}
    target = {key: f"Deutscher Text {i}." for i, key in enumerate(source)}
    target["message.0"] = source["message.0"]
    for filename, values in [("displayStrings.properties", source), ("displayStrings_de.properties", target)]:
        (resources / filename).write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    relative = "core/src/main/resources/i18n"
    changed = relative + "/displayStrings_de.properties"
    return SimpleNamespace(repo=repo, resources=resources, relative=relative, changed=changed,
                           source=source, elsewhere=elsewhere)


def test_real_159_entry_diff_hydrates_relative_input_from_outside_checkout(external_checkout):
    case = external_checkout
    diff = get_staged_diff(str(case.repo), [case.changed])
    assert diff.strip()
    changes = list(iter_translation_changes_from_diff(diff, str(case.repo), case.relative, ["de"]))
    assert len(changes) == 159
    assert all(change.source_value == case.source[change.key] for change in changes)
    assert {change.file for change in changes} == {"displayStrings_de.properties"}


def test_full_audit_resolves_relative_folder_against_repo(external_checkout):
    case = external_checkout
    changes = list(iter_all_translation_entries(str(case.repo), case.relative, ["de"]))
    assert len(changes) == 159
    assert all(change.source_value == case.source[change.key] for change in changes)


def gate_args(case):
    config = case.elsewhere / "config.yaml"
    config.write_text(yaml.safe_dump({"supported_locales": [{"code": "de", "name": "German"}]}))
    return ["--repo-root", str(case.repo), "--input-folder", case.relative, "--config", str(config),
            "--validation-summary", str(case.elsewhere / "validation.json"),
            "--output-json", str(case.elsewhere / "quality.json"),
            "--output-markdown", str(case.elsewhere / "quality.md"), "--changed-files", case.changed]


def test_gate_checks_real_diff_and_blocks_untranslated_prose_outside_checkout(external_checkout):
    case = external_checkout
    assert main(gate_args(case)) == 1
    report = json.loads((case.elsewhere / "quality.json").read_text())
    assert report["source_identical"]["changed_entries_count"] == 159
    assert report["source_identical"]["checked_entries_count"] == 159
    assert report["source_identical"]["new_source_identical_prose_count"] == 1


def test_gate_missing_source_cannot_report_zero_coverage_success(external_checkout):
    case = external_checkout
    (case.resources / "displayStrings.properties").unlink()
    with pytest.raises(RuntimeError, match="source.*unavailable|source.*missing"):
        main(gate_args(case))
    assert not (case.elsewhere / "quality.json").exists()


def test_strict_diff_hydration_rejects_missing_source_before_semantic_requests(external_checkout):
    case = external_checkout
    (case.resources / "displayStrings.properties").unlink()
    diff = get_staged_diff(str(case.repo), [case.changed])
    with pytest.raises(RuntimeError, match="source.*unavailable|source.*missing"):
        list(iter_translation_changes_from_diff(diff, str(case.repo), case.relative, ["de"], require_source=True))


def test_source_affected_audit_resolves_relative_folder_before_git(external_checkout, monkeypatch):
    case = external_checkout
    def run(command, **kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout="" if "diff" in command else "a" * 40)
    monkeypatch.setattr("localize.translation_quality_gate.subprocess.run", run)
    profiles = [LocalizationProfile(JAVA_PROPERTIES_FORMAT, SUFFIX_LAYOUT)]
    assert source_affected_translation_entries(str(case.repo), case.relative, ["de"], profiles, "HEAD") == []
