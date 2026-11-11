"""The manual Bisq 1 job must preserve its layout and catch changed English."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bisq1_local", ROOT / "scripts" / "run-bisq1-local.py"
)
job = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job)


def test_profile_discovers_all_existing_bisq1_locales():
    from localize.formats.profiles import load_localization_profiles

    config = yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())
    codes = [locale["code"] for locale in config["supported_locales"]]
    assert set(codes) == {
        "cs", "de", "es", "fa", "fr", "it", "ja", "pl", "pt-br",
        "pt", "ru", "th", "vi", "zh-hans", "zh-hant",
    }
    profile, = load_localization_profiles(config)
    for code in codes:
        filename = f"displayStrings_{code}.properties"
        assert profile.localization_layout.is_target_file(
            filename, codes, profile.localization_format
        )
        assert profile.localization_layout.source_path_for_target(
            filename, codes, profile.localization_format
        ) == "displayStrings.properties"
    assert not profile.localization_layout.is_target_file(
        "displayStrings.properties", codes, profile.localization_format
    )


def test_baseline_selects_changed_english_without_rewriting_existing_translation(tmp_path):
    from localize import translate_localization_files as runtime

    source = tmp_path / "source.properties"
    target = tmp_path / "displayStrings_de.properties"
    source.write_text("warning=Old security warning\nstable=Account\n")
    target.write_text("warning=Alte Sicherheitswarnung\nstable=Konto\n")
    old_source = job.parse_properties_file(str(source))[1]
    lines, targets = job.parse_properties_file(str(target))
    ledger = runtime.build_file_key_ledger(old_source, targets)
    before = target.read_bytes()
    _, _, keys = runtime.extract_texts_to_translate(
        lines, {"warning": "New security warning", "stable": "Account"},
        targets, file_ledger_entries=ledger,
    )
    assert keys == ["warning"]
    assert target.read_bytes() == before


@pytest.mark.parametrize("skipped", [{"displayStrings_de.properties": ["invalid placeholder"]}])
def test_partial_success_is_failure(skipped):
    with pytest.raises(RuntimeError, match="skipped"):
        job.require_complete_run(SimpleNamespace(
            changed_files=["displayStrings_de.properties", "displayStrings_fr.properties"],
            processed_files_count=1, skipped_files=skipped,
        ))


def test_already_translated_file_is_a_successful_noop():
    job.require_complete_run(SimpleNamespace(
        changed_files=["displayStrings_de.properties"],
        processed_files_count=0, skipped_files={},
    ))


def test_configuration_isolates_reports_and_keeps_quality_guards(tmp_path):
    repo = tmp_path / "bisq"
    state = tmp_path / "state"
    state.mkdir()
    config = job.write_config(repo, state, ["pt-br", "zh-hant"])
    payload = yaml.safe_load(config.read_text())
    assert payload["target_project_root"] == str(repo)
    assert {row["code"] for row in payload["supported_locales"]} == {"pt-br", "zh-hant"}
    assert payload["process_all_files"] is True
    assert payload["quality_gate"]["block_on_pipeline_warnings"] is True
    assert payload["quality_gate"]["block_on_semantic_qa_findings"] is True
    assert payload["translation_key_ledger_file_path"] == str(state / "ledger.json")
    assert json.loads(Path(payload["glossary_file_path"]).read_text()) == {}


def test_unknown_locale_fails_before_running(tmp_path):
    with pytest.raises(ValueError, match="locale"):
        job.write_config(tmp_path / "bisq", tmp_path, ["pt_BR"])


def test_adding_locale_seeds_missing_baseline_and_preserves_existing_ledger(tmp_path, monkeypatch):
    from localize import translate_localization_files as runtime

    repo = tmp_path / "bisq"
    resources = repo / job.RESOURCE_PATH
    resources.mkdir(parents=True)
    (resources / "displayStrings_pt-br.properties").write_text("warning=Aviso antigo\n")
    existing = {"displayStrings_de.properties": {
        "warning": {"source_hash": "keep-source", "target_hash": "keep-target", "status": "failed"},
    }}
    (tmp_path / "ledger.json").write_text(json.dumps({"version": 1, "files": existing}))
    before = (tmp_path / "ledger.json").read_bytes()
    monkeypatch.setattr(job, "git", lambda _repo, *args:
                        "warning=Old warning\n" if args[0] == "show" else "a" * 40)
    monkeypatch.setattr(job.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    ledger = job.baseline_ledger(
        runtime, repo, tmp_path, "baseline",
        ["displayStrings_de.properties", "displayStrings_pt-br.properties"],
    )
    assert ledger["displayStrings_de.properties"] == existing["displayStrings_de.properties"]
    assert ledger["displayStrings_pt-br.properties"]["warning"]["source_hash"] == runtime.compute_ledger_hash("Old warning")
    assert (tmp_path / "ledger.json").read_bytes() == before


def test_pending_changes_stop_checkout_before_fetch(tmp_path, monkeypatch):
    calls = []

    def fake_git(repo, *args):
        calls.append(args)
        return {
            ("remote", "get-url", "origin"): job.UPSTREAM,
            ("branch", "--show-current"): job.BRANCH,
            ("status", "--porcelain"): " M core/src/main/resources/i18n/displayStrings_de.properties",
        }[args]

    monkeypatch.setattr(job, "git", fake_git)
    with pytest.raises(RuntimeError, match="pending changes"):
        job.prepare_checkout(tmp_path)
    assert not any(args[0] == "fetch" for args in calls)


def test_default_workspace_is_ignored_by_git(tmp_path):
    """The default workspace holds a sparse checkout, state and a private .env."""
    import shutil
    import subprocess

    # Use a clean repository so local excludes cannot hide a missing rule.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    default = job.ROOT / ".agent-artifacts/bisq1-local"
    for path in (default / ".env", default / "bisq/README.md"):
        relative = path.relative_to(ROOT)
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--no-index", "-q", str(relative)],
            check=False,
        )
        assert result.returncode == 0, f"{relative} is not ignored"


def test_profile_uses_evaluated_models_with_valid_settings():
    from localize.app_config import validate_config

    config = yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())
    assert (config["model_name"], config["review_model_name"],
            config["semantic_review"]["model"]) == ("gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna")
    assert config["model_max_completion_tokens"] == 4096
    errors = [
        issue.message for issue in validate_config(
            config, path_exists=lambda path: True, api_key_available=True,
        ) if issue.level == "error"
    ]
    assert errors == []
